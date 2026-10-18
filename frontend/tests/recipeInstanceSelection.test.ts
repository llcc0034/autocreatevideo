import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createRenderer,createSSRApp,h,nextTick,reactive,ssrContextKey} from 'vue'
import {renderToString} from '@vue/server-renderer'

const sha='a'.repeat(64), id='fixture-structure', other='fixture-other'
const options={definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:4},stageOptions:[{
  value:'bible_image_generation',label:'素材',repeatable:false,candidateEvaluatorOptions:[],modes:['existing_pipeline'],routingModes:['existing_pipeline']}],
  interactionModeOptions:[{value:'assisted'},{value:'unattended'}],approvalOptions:[{value:'auto'}],
  routingModeOptions:['fixed','ordered_fallback','manual_choice'].map(value=>({value,label:value})),
  materialSourceOptions:['generate','existing','upload'].map(value=>({value,label:value})),materialSelectionOptions:[],
  fieldReviewPolicyOptions:[],sourceKindOptions:[],structureNodeOptions:[],textOutputOptions:[],referenceSourceOptions:[],parameterPolicyOptions:[],parameterSourceOptions:[],scopeOptions:[],presets:[]}
const route=(version=id)=>({execution_instance_version_id:version,expected_definition_sha256:sha,input_sources:{raw:{kind:'literal',value:' 原文\r\n🌧 '}},parameter_policies:[{key:'seed',policy:'locked',value:0}]})
const group=()=>({mode:'fixed',variant_count:2,evaluator:'first_succeeded',routes:[route()]})
const draft=()=>({schema_version:3,interaction_mode:'assisted',stages:[{key:'bible_image_generation',mode:'existing_pipeline',approval:'auto'}]})
const copy=value=>JSON.parse(JSON.stringify(value))
async function mounted(fn, definition:any=draft()){
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const {executionRegistryApi:api}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const {recipeDefaultContract}=await vite.ssrLoadModule('/tests/fixtures/recipe-default-data.ts')
  const original={versions:api.versions,contract:api.contract};const requests:any[]=[]
  api.versions=async(page,filters)=>{requests.push({page,filters});return{results:[id,other].map(id=>({id,instance_name:id,version:1,status:'published'})),next:null}}
  api.contract=async(version)=>{const contract=recipeDefaultContract();return {...contract,version_id:version,definition:{...contract.definition,version_id:version}}}
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionRecipeEditor.vue')
  const props:any=reactive({disabled:false,modelValue:copy(definition),options:copy(options)}), writes:any[]=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event,value)=>{if(event==='update:modelValue'){writes.push(copy(value));props.modelValue=value}}});return()=>h('div')}})
  try{app.provide(ssrContextKey,{modules:new Set()});app.mount({});await nextTick();await fn({state,props,writes,requests,api,app,recipeDefaultContract,Component})}
  finally{app.unmount();Object.assign(api,original);await vite.close()}
}
async function open(state,key='routing',index:number|null=null){state.openInstancePicker(0,key,index);await Promise.resolve();await nextTick()}

test('step-local selection reads catalog only on demand and directly creates one exact instance',()=>mounted(async({state,props,writes,requests})=>{
  assert.equal(requests.length,0);assert.equal(writes.length,0)
  await open(state,'variant_routing');await state.selectInstance(id);await nextTick()
  assert.equal(requests.length,1);assert.equal(writes.length,1)
  assert.equal(props.modelValue.stages[0].routing,undefined)
  assert.deepEqual(props.modelValue.stages[0].variant_routing,{mode:'fixed',variant_count:1,evaluator:null,routes:[{
    execution_instance_version_id:id,expected_definition_sha256:sha,input_sources:{},parameter_policies:[]}]})
  assert.equal(state.instancePicker.value,null)
}))

test('same exact instance is a no-op; replacement requires confirmation and only clears affected mappings',()=>{
  const value:any=draft();value.stages[0].routing=group();value.stages[0].variant_routing=group()
  return mounted(async({state,props,writes})=>{
    await open(state,'routing',0);await state.selectInstance(id);assert.equal(writes.length,0)
    await open(state,'routing',0);await state.selectInstance(other);assert.equal(writes.length,0)
    state.pendingStageEdit.value=null;state.confirmStageEdit();assert.equal(writes.length,0)
    await open(state,'routing',0);await state.selectInstance(other);state.confirmStageEdit();await nextTick()
    assert.equal(writes.length,1);assert.deepEqual(props.modelValue.stages[0].variant_routing,value.stages[0].variant_routing)
    assert.equal(props.modelValue.stages[0].routing.variant_count,2);assert.equal(props.modelValue.stages[0].routing.evaluator,'first_succeeded')
    assert.deepEqual(props.modelValue.stages[0].routing.routes[0].input_sources,{})
    assert.deepEqual(props.modelValue.stages[0].routing.routes[0].parameter_policies,[])
  },value)
})

test('cancelled, readonly or changed draft never accepts a late contract response',async()=>{
  for(const action of ['cancel','readonly','draft','options'])await mounted(async({state,props,writes,api,recipeDefaultContract})=>{
    await open(state);let resolve:any
    api.contract=()=>new Promise(done=>resolve=done)
    const pending=state.selectInstance(id)
    if(action==='cancel')state.closeInstancePicker()
    if(action==='readonly')props.disabled=true
    if(action==='draft')props.modelValue=copy({...props.modelValue,interaction_mode:'unattended'})
    if(action==='options')props.options=copy({...props.options,effectiveLimits:{maxRoutingRoutes:1,maxVariants:1}})
    await nextTick();resolve(recipeDefaultContract());await pending;assert.equal(writes.length,0)
  })
})

test('catalog failure clears stale choices, supports retry and cannot invalidate or change saved configuration',()=>mounted(async({state,props,writes,api})=>{
  const before=copy(props.modelValue);await open(state)
  api.versions=async()=>{throw new Error('catalog unavailable')};await state.loadVersions(2)
  assert.equal(state.versions.value.length,0);assert.match(state.error.value,/catalog unavailable/)
  await state.selectInstance(id);assert.equal(writes.length,0);assert.deepEqual(props.modelValue,before)
  api.versions=async()=>({results:[{id}],next:null});await state.loadVersions(1);assert.equal(state.error.value,'')
  state.closeInstancePicker();assert.equal(writes.length,0)
}))

test('ordered candidates can reorder intact; switching to single requires explicit retained item',()=>{
  const value:any=draft();value.stages[0].routing={...group(),mode:'ordered_fallback',routes:[route(),route(other)]}
  return mounted(async({state,props,writes})=>{
    state.moveInstance(0,'routing',1,-1);await nextTick()
    assert.deepEqual(props.modelValue.stages[0].routing.routes,[route(other),route()])
    const input={value:'fixed'};state.requestRoutingMode(0,'routing',{target:input})
    assert.equal(input.value,'ordered_fallback');state.confirmSingleInstance();assert.equal(writes.length,1)
    state.pendingSingleInstance.value.keep=1;state.confirmSingleInstance();await nextTick()
    assert.deepEqual(props.modelValue.stages[0].routing,{...group(),routes:[route()]})
    props.disabled=true;await nextTick();state.moveInstance(0,'routing',0,1);state.updateRoute(0,'routing',0,null)
    assert.equal(writes.length,2)
  },value)
})

test('multi-to-single cancellation and stale confirmation preserve every saved candidate',()=>{
  const value:any=draft();value.stages[0].routing={...group(),mode:'manual_choice',routes:[route(),route(other)]}
  return mounted(async({state,props,writes})=>{
    state.requestRoutingMode(0,'routing',{target:{value:'fixed'}});state.pendingSingleInstance.value=null;state.confirmSingleInstance()
    assert.equal(writes.length,0)
    state.requestRoutingMode(0,'routing',{target:{value:'fixed'}});state.pendingSingleInstance.value.keep=1
    props.modelValue=copy({...props.modelValue,interaction_mode:'unattended'});state.confirmSingleInstance()
    assert.equal(writes.length,0);assert.deepEqual(props.modelValue.stages[0].routing,value.stages[0].routing)
    await nextTick();state.requestRoutingMode(0,'routing',{target:{value:'manual_choice'}});assert.equal(writes.length,0)
  },value)
})

test('missing catalog limits and mismatched returned identity fail without writing a hidden selection',async()=>{
  for(const reason of ['limits','identity'])await mounted(async({state,props,writes,api,recipeDefaultContract})=>{
    if(reason==='limits')props.options.effectiveLimits.maxRoutingRoutes=0
    await open(state)
    if(reason==='identity')api.contract=async()=>({...recipeDefaultContract(),version_id:'wrong'})
    await state.selectInstance(id);assert.equal(writes.length,0);assert.ok(state.error.value)
  })
})

test('duplicate selection waits for one contract and cancelled catalog results cannot repopulate choices',()=>mounted(async({state,writes,api,recipeDefaultContract})=>{
  await open(state);let finish:any,count=0
  api.contract=()=>{count++;return new Promise(resolve=>finish=resolve)}
  const selection=state.selectInstance(id);await state.selectInstance(id)
  assert.equal(count,1);finish(recipeDefaultContract());await selection;await nextTick();assert.equal(writes.length,1)
  api.versions=()=>new Promise(resolve=>finish=resolve)
  state.openInstancePicker(0,'routing',0);state.closeInstancePicker()
  finish({results:[{id}],next:null});await Promise.resolve();await nextTick()
  assert.equal(state.instancePicker.value,null);assert.deepEqual(state.versions.value,[]);assert.equal(writes.length,1)
}))

test('single instance hides advanced selection by default and published details expose no edit actions',()=>{
  const value:any=draft();value.stages[0].routing=group()
  return mounted(async({Component,props,requests})=>{
    const html=await renderToString(createSSRApp(Component,{...props,disabled:true}))
    const advanced=html.match(/<details([^>]*)><summary[^>]*>高级设置：多实例切换/)
    assert.ok(advanced);assert.equal(/\bopen\b/.test(advanced[1]),false)
    for(const text of ['更换执行实例','移除执行实例','选择主图执行实例','添加备选执行实例','配置路线'])assert.equal(html.includes(text),false,text)
    assert.ok(html.includes('所选执行实例'));assert.equal(requests.length,0)
  },value)
})
