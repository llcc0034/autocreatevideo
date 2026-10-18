import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createSSRApp, createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url))
const native = key => ({value:key,label:key,repeatable:false,candidateEvaluatorOptions:[],modes:['review'],requiresRouting:false,routingModes:[],decisionPolicy:null})
const generic = {value:'structured_text',label:'通用结构化文本',repeatable:true,modes:['ai'],requiresRouting:true,routingModes:['ai'],decisionPolicy:null,
  candidateEvaluatorOptions:[{value:null,label:'人工选择候选'},{value:'first_succeeded',label:'按候选序号自动选择成功项'}]}
const options = {definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:4,maxGenerationTasks:30},stageOptions:[native('script_review'),native('storyboard_review'),generic],
  interactionModeOptions:[{value:'assisted',label:'人工辅助'},{value:'unattended',label:'无人值守'}],approvalOptions:[{value:'auto',label:'自动'},{value:'required',label:'人工审核'}],
  routingModeOptions:[{value:'fixed',label:'固定路线'}],fieldReviewPolicyOptions:[],sourceKindOptions:[],structureNodeOptions:[],textOutputOptions:[],referenceSourceOptions:[],
  parameterPolicyOptions:[],parameterSourceOptions:[],scopeOptions:[],presets:[]}
const step = key => ({key,kind:'structured_text',name:'同名步骤',mode:'ai',approval:'auto',routing:{mode:'fixed',variant_count:1,evaluator:null,routes:[]}})
const value = stages => ({schema_version:3,interaction_mode:'assisted',stages})
async function modules(fn) {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}})
  try { await fn(await vite.ssrLoadModule('/src/services/productionRecipeAuthoring.ts'),vite) } finally { await vite.close() }
}

test('repeatable duty gets unique identity only on explicit creation; rename or same duty does not recreate it',()=>modules(async m=>{
  let sequence=0
  const a=m.recipeStageWithKind({key:'',mode:'',approval:''},'structured_text',options,()=>`step-${++sequence}`)
  const b=m.recipeStageWithKind({},'structured_text',options,()=>`step-${++sequence}`)
  assert.notEqual(a.key,b.key);assert.equal(a.kind,b.kind);assert.equal(a.name,b.name)
  a.name='修改显示名'
  assert.equal(m.recipeStageWithKind(a,'structured_text',options,()=>{throw Error('must not rekey')}),a)
  assert.deepEqual(m.recipeAuthoringErrors(value([a,b]),options),[])
  assert.deepEqual(m.recipeStageWithKind(a,'script_review',options,()=>''),{key:'script_review',mode:'',approval:''})
}))

test('moving uses configured order, preserves references and native order; duplicate labels are allowed',()=>modules(async m=>{
  const nativeA={key:'script_review',mode:'review',approval:'required'},nativeB={key:'storyboard_review',mode:'review',approval:'required'}
  const a=step('first'),b=step('second'),stages=[nativeA,a,b,nativeB],before=structuredClone(stages)
  assert.deepEqual(m.recipeAuthoringErrors(value(stages),options),[])
  const moved=m.moveRecipeStage(stages,1,-1,options)
  assert.deepEqual(moved,[a,nativeA,b,nativeB]);assert.deepEqual(stages,before)
  assert.equal(m.moveRecipeStage([nativeA,nativeB],0,1,options),null)
  assert.equal(m.moveRecipeStage(stages,0,-1,options),null)
  for(const bad of [[a,a],[{...a,key:'script_review'}],[{...a,name:''}],[nativeB,a,nativeA]]) {
    assert.ok(m.recipeAuthoringErrors(value(bad),options).length)
  }
}))

test('candidate mode follows server options and unattended cannot wait for manual selection',()=>modules(async m=>{
  const a=step('first')
  assert.deepEqual(m.recipeAuthoringErrors(value([a]),options),[])
  assert.match(m.recipeAuthoringErrors({...value([a]),interaction_mode:'unattended'},options).join(' '),/人工选择候选/)
  a.routing.evaluator='first_succeeded'
  assert.deepEqual(m.recipeAuthoringErrors({...value([a]),interaction_mode:'unattended'},options),[])
  assert.match(m.recipeAuthoringErrors(value([{...a,approval:'required'}]),options).join(' '),/不另设阶段审核/)
  a.routing.evaluator='quality_rank'
  assert.match(m.recipeAuthoringErrors(value([a]),options).join(' '),/选择方式/)
}))

test('real editor renders same-name identities, selection mode and readonly controls without dispatch',()=>modules(async (_,vite)=>{
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionRecipeEditor.vue')
  const input=value([step('first'),step('second')])
  input.stages[1].routing.evaluator='first_succeeded'
  const html=await renderToString(createSSRApp(Component,{modelValue:input,options,disabled:true}))
  for(const text of ['同名步骤','first','second','通用结构化文本','人工选择候选','按候选序号自动选择成功项','改名不改变步骤身份']) assert.ok(html.includes(text),text)
  assert.equal(html.includes('未识别阶段'),false);assert.equal(html.includes('阶段审核<select'),false)
  assert.equal(html.includes('上移步骤'),false)
}))

test('real editor local changes keep IDs and data, catalog read errors remain visible',()=>modules(async (_,vite)=>{
  const {executionRegistryApi:api}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  api.versions=async()=>{throw new Error('隔离目录读取失败')}
  const {default:Component}=await vite.ssrLoadModule('/src/components/ProductionRecipeEditor.vue')
  const props=reactive({modelValue:value([step('first'),step('second')]),options,disabled:false})
  let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event,data)=>{if(event==='update:modelValue')props.modelValue=data}});return()=>h('div')}})
  try {
    app.provide(ssrContextKey,{modules:new Set()});app.mount({});await Promise.resolve();await nextTick()
    assert.equal(state.error.value,'')
    state.openInstancePicker(0,'routing');await Promise.resolve();await nextTick()
    assert.match(state.error.value,/目录读取失败/)
    state.updateStage(0,{name:'另一名称'});state.moveStage(0,1);await nextTick()
    assert.deepEqual(props.modelValue.stages.map(row=>row.key),['second','first'])
    assert.equal(props.modelValue.stages[1].name,'另一名称')
    assert.equal(props.modelValue.stages[1].routing.evaluator,null)
    props.disabled=true;await nextTick();state.moveStage(0,1)
    assert.deepEqual(props.modelValue.stages.map(row=>row.key),['second','first'])
  } finally {app.unmount()}
}))

test('frozen creation summary preserves same-name step identity and duty',()=>modules(async (_,vite)=>{
  const {recipeExecutionOverview}=await vite.ssrLoadModule('/src/services/recipeExecutionOverview.ts')
  const {default:Component}=await vite.ssrLoadModule('/src/components/RecipeExecutionSummary.vue')
  const a=step('first'),b=step('second')
  delete a.routing;delete b.routing
  const definition=value([a,b]),overview=recipeExecutionOverview(definition)
  assert.equal(overview.error,'')
  assert.deepEqual(overview.stages.map(row=>[row.key,row.kind,row.name]),[['first','structured_text','同名步骤'],['second','structured_text','同名步骤']])
  const html=await renderToString(createSSRApp(Component,{recipe:{definition}}))
  for(const text of ['同名步骤','步骤身份 first','步骤身份 second'])assert.ok(html.includes(text))
  assert.equal(html.includes('未识别阶段'),false)
}))
