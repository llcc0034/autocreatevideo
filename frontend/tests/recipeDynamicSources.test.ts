import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createRenderer,h,nextTick,reactive,ssrContextKey} from 'vue'

const root=fileURLToPath(new URL('..',import.meta.url))
async function modules(fn){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}});try{await fn(vite)}finally{await vite.close()}}
function mount(Component,props,emit=()=>{}){let state:any;const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}});const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit});return()=>h('div')}});app.provide(ssrContextKey,{modules:new Set()});app.mount({});return{state,unmount:()=>app.unmount()}}
const output=(stageKey, schema={type:'object',properties:{count:{type:'integer'},active:{type:'boolean'}}})=>({stageKey,outputKey:'structured_text.same-version',kind:'json',scopeKind:'project',label:'同名步骤',value_schema:schema})
const node=(stage_key)=>({kind:'selected_output',stage_key,output_key:'structured_text.same-version',scope:{kind:'project'},selection_policy:'reviewed_selected'})
const stage=(key)=>({key,kind:'structured_text',name:'同名步骤',mode:'ai',approval:'auto',routing:{mode:'fixed',variant_count:1,evaluator:null,routes:[{execution_instance_version_id:'fixture',expected_definition_sha256:'a'.repeat(64),input_sources:{},parameter_policies:[]}]}})
const options={definitionSchemaVersion:3,effectiveLimits:{maxRoutingRoutes:3,maxVariants:3,maxGenerationTasks:20},stageOptions:[{value:'structured_text',label:'通用结构化文本',repeatable:true,modes:['ai'],routingModes:['ai'],candidateEvaluatorOptions:[{value:null,label:'人工选择'}]}],textOutputOptions:[],sourceKindOptions:[],presets:[],interactionModeOptions:[{value:'assisted',label:'辅助制作'}],approvalOptions:[{value:'auto',label:'自动推进'}],routingModeOptions:[{value:'fixed',label:'固定路线'}]}
const tick=async()=>{await Promise.resolve();await nextTick();await Promise.resolve();await nextTick()}

test('mapping schema and field picker resolve step+output, not first output with same key',()=>modules(async vite=>{
  const m=await vite.ssrLoadModule('/src/services/recipeStructureMapping.ts')
  const outputs=[output('one'),output('two',{type:'array',items:{type:'string'}})],catalog={fields:[],outputs}
  assert.deepEqual(m.mappingSourceSchema(node('two'),catalog),outputs[1].value_schema)
  assert.match(m.mappingValueSchema(node('missing'),catalog).error,/当前目录/)
  assert.match(m.mappingErrors({...node('one'),path:['missing']},{},'x',[],catalog).join(' '),/来源|路径|字段/)
  assert.notEqual(m.mappingOutputIdentity('one',outputs[0].outputKey),m.mappingOutputIdentity('two',outputs[0].outputKey))
  const {default:C}=await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')
  const props=reactive({modelValue:node('one'),schema:{},label:'input',nodes:[],fields:[],outputs,scopes:['project'],priorStages:['one','two'],disabled:false})
  const app=mount(C,props,(event,data)=>{props.modelValue=data})
  try{
    app.state.selectOutput(m.mappingOutputIdentity('two',outputs[1].outputKey));await tick()
    assert.equal(props.modelValue.stage_key,'two');assert.deepEqual(app.state.sourceSchema.value,outputs[1].value_schema)
    props.priorStages=['one'];await tick()
    assert.equal(app.state.selectedOutput.value,undefined);assert.equal(props.modelValue.stage_key,'two')
    assert.deepEqual(app.state.sourceSchema.value,{})
  }finally{app.unmount()}
}))

test('server catalog adapter rejects duplicate pairs but permits same output in two steps',()=>modules(async vite=>{
  const {adaptRecipeTextOutputOptions:adapt}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const a={stage_key:'one',output_key:'shared',kind:'json',scope_kind:'project',label:'1 · 同名',value_schema:{type:'array',items:{type:'boolean'}}}
  assert.equal(adapt({text_output_options:[a,{...a,stage_key:'two'}]}).length,2)
  assert.throws(()=>adapt({text_output_options:[a,a]}),/重复/)
  assert.throws(()=>adapt({}),/text_output_options/)
  assert.throws(()=>adapt({text_output_options:[{...a,value_schema:null}]}),/value_schema/)
}))

test('catalog projection does not reload for mapped values, but changes for output, order and label',()=>modules(async vite=>{
  const {recipeSourceCatalogDefinition:project}=await vite.ssrLoadModule('/src/services/productionRecipeAuthoring.ts')
  const a={schema_version:3,interaction_mode:'assisted',stages:[stage('one'),stage('two')]},before=project(a)
  a.stages[1].routing.routes[0].input_sources={root:{kind:'literal',value:{count:0}}}
  a.stages[1].routing.evaluator='first_succeeded'
  assert.deepEqual(project(a),before)
  a.stages.reverse();assert.notDeepEqual(project(a),before)
  a.stages.reverse();a.stages[0].name='改名';assert.notDeepEqual(project(a),before)
}))

test('whole output selection keeps the chosen step when output versions are reused',()=>modules(async vite=>{
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const {recipeDefaultContract,recipeDefaultOptions}=await vite.ssrLoadModule('/tests/fixtures/recipe-default-data.ts')
  const contract=recipeDefaultContract();contract.definition.configuration.inputs[0].semantic_contract.allowed_scope_kinds=['project']
  executionRegistryApi.contract=async()=>contract
  const {default:C}=await vite.ssrLoadModule('/src/components/ProductionRecipeRouteEditor.vue')
  const props=reactive({modelValue:{execution_instance_version_id:contract.version_id,expected_definition_sha256:contract.sha256,input_sources:{custom:node('two')},parameter_policies:[]},
    options:{...recipeDefaultOptions,textOutputOptions:[output('one'),output('two')]},priorStages:[{key:'one'},{key:'two'}]})
  const app=mount(C,props,(_,value)=>{if(value?.input_sources)props.modelValue=value})
  try{await tick();app.state.setTextOutput('custom','structured_text.same-version');assert.equal(props.modelValue.input_sources.custom.stage_key,'two')}
  finally{app.unmount()}
}))

test('real recipe editor discards stale replies, exposes retry, and never rewrites saved mappings',()=>modules(async vite=>{
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  executionRegistryApi.versions=async()=>({results:[],next:null})
  const pending:any[]=[];platformApi.getRecipeSourceOptions=definition=>new Promise((resolve,reject)=>pending.push({definition,resolve,reject}))
  const {default:C}=await vite.ssrLoadModule('/src/components/ProductionRecipeEditor.vue')
  const props=reactive({modelValue:{schema_version:3,interaction_mode:'assisted',stages:[stage('one'),stage('two')]},options,disabled:false})
  props.modelValue.stages[1].routing.routes[0].input_sources={root:node('one')}
  const app=mount(C,props);const before=structuredClone(JSON.parse(JSON.stringify(props.modelValue)))
  try{
    assert.equal(pending.length,1);assert.equal(app.state.sourceLoading.value,true)
    props.modelValue.stages[0].name='新名称';await tick();assert.equal(pending.length,2)
    pending[1].resolve([output('one'),output('two')]);await tick()
    assert.equal(app.state.routeOptions(1).textOutputOptions.length,1)
    assert.equal(app.state.routeOptions(0).textOutputOptions.length,0)
    pending[0].resolve([output('outdated')]);await tick()
    assert.equal(app.state.sourceOutputs.value[0].stageKey,'one')
    void app.state.loadSourceOptions();assert.equal(app.state.sourceOutputs.value.length,0)
    pending[2].reject(new Error('隔离目录读取失败'));await tick()
    assert.match(app.state.sourceError.value,/读取失败/)
    assert.deepEqual(props.modelValue.stages[1],before.stages[1])
    void app.state.loadSourceOptions();pending[3].resolve([]);await tick()
    assert.equal(app.state.sourceError.value,'');assert.equal(app.state.sourceLoading.value,false)
    props.modelValue.stages.reverse();await tick();assert.equal(pending.length,5)
    app.unmount();pending[4].resolve([output('after-unmount')]);await tick()
    assert.deepEqual(app.state.sourceOutputs.value,[])
  }finally{app.unmount()}
}))
