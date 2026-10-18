import assert from 'node:assert/strict'
import test from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer} from 'vite'
import {createRenderer,h,nextTick,reactive,ssrContextKey} from 'vue'

const root=fileURLToPath(new URL('..',import.meta.url)),copy=(value:any)=>JSON.parse(JSON.stringify(value))
const tick=async()=>{await Promise.resolve();await nextTick();await Promise.resolve();await nextTick()}
async function mounted(fn,routeFactory=(data)=>data.ownershipRoute(),component='ProductionRecipeRouteEditor',extraFactory=()=>({})){
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const data=await vite.ssrLoadModule('/tests/fixtures/input-ownership-data.ts')
  const api=(await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')).executionRegistryApi
  const previous=api.contract;api.contract=async()=>data.ownershipContract()
  const Component=(await vite.ssrLoadModule(`/src/components/${component}.vue`)).default
  const props=reactive({modelValue:copy(routeFactory(data)),options:copy(data.ownershipOptions),priorStages:[],disabled:false,...extraFactory(data)})
  const writes:any[]=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event,value)=>{if(event==='update:modelValue'){writes.push(copy(value));props.modelValue=value}}});return()=>h('div')}})
  try{app.provide(ssrContextKey,{modules:new Set()});app.mount({});await tick();await fn({state,props,writes,api})}
  finally{app.unmount();api.contract=previous;await vite.close()}
}

test('recipe starts with inherited declarations and selecting an override creates no duplicate configuration',()=>mounted(async({state,writes})=>{
  assert.equal(state.inheritedParameters.value.length,5);assert.deepEqual(state.configuredParameters.value,[])
  assert.deepEqual(state.addableParameters.value.map(row=>row.key),['width','sound','missing'])
  state.selectedParameter.value='width';await tick()
  assert.equal(state.configuredParameters.value[0].key,'width');assert.equal(writes.length,0)
  state.setPolicyKind('width','default',state.contract.value.parameters[0].rule)
  assert.deepEqual(writes.at(-1).parameter_policies,[{key:'width',policy:'default',value:0}])
  assert.ok(!state.addableParameters.value.some(row=>row.key==='width'))
}))
test('explicit overrides preserve false and zero; restoring inheritance removes only the selected step rule',()=>mounted(async({state,props,writes})=>{
  const sources=copy(props.modelValue.input_sources)
  for(const key of ['width','sound'])state.setPolicyKind(key,'locked_value',state.contract.value.parameters.find(row=>row.key===key).rule)
  assert.equal(props.modelValue.parameter_policies[1].value,false)
  state.selectedParameter.value='width';state.setPolicy('width',null)
  assert.equal(state.selectedParameter.value,'')
  assert.deepEqual(props.modelValue.parameter_policies,[{key:'sound',policy:'locked_value',value:false}])
  assert.deepEqual(props.modelValue.input_sources,sources);assert.equal(writes.length,3)
}))
test('target constants, generated seeds, unknown parameters and readonly/error states cannot gain overrides',()=>mounted(async({state,props,writes})=>{
  for(const key of ['fixed','seed','absent'])state.setPolicyKind(key,'locked_value',{default:123})
  assert.equal(writes.length,0)
  props.disabled=true;state.setPolicyKind('width','default',{});state.setPolicy('width',null)
  props.disabled=false;state.error.value='合同读取失败';state.setPolicyKind('width','default',{})
  assert.equal(writes.length,0)
}))
test('invalid stored rules remain visible and are removed only explicitly',()=>{
  return mounted(async({state,props,writes})=>{
    assert.equal(state.unknownPolicies.value[0].key,'old');assert.equal(state.configuredParameters.value[0].key,'fixed')
    assert.equal(writes.length,0);state.setPolicy('old',null)
    assert.deepEqual(props.modelValue.parameter_policies,[{key:'fixed',policy:'locked_value',value:2}])
  },data=>({...data.ownershipRoute(),parameter_policies:[{key:'old',policy:'default',value:'original'},{key:'fixed',policy:'locked_value',value:2}]}))
})
test('changing exact instance discards only the unsaved parameter picker, never rewrites saved rules',()=>mounted(async({state,props,writes})=>{
  state.selectedParameter.value='width';props.modelValue={...props.modelValue,expected_definition_sha256:'changed'};await tick()
  assert.equal(state.selectedParameter.value,'');assert.match(state.error.value,/摘要不一致/);assert.equal(writes.length,0)
}))

test('instance add menus exclude connected targets, derived inputs, configured parameters and target constants',()=>{
  const target=(key)=>({target_kind:'workflow_slot',target_key:key,kind:'image',required:true,min_items:1,max_items:1})
  return mounted(async({state,props,writes})=>{
    assert.deepEqual(state.addableTargets.value.map(row=>row.target_key),['image2'])
    assert.ok(!state.addableParameters.value.some(row=>['width','fixed'].includes(row.key)))
    state.selectedTarget.value=JSON.stringify(['workflow_slot','image']);state.addInput();assert.equal(writes.length,0)
    state.selectedTarget.value=JSON.stringify(['workflow_slot','image2']);state.addInput();assert.equal(writes.length,1)
    assert.deepEqual(state.addableTargets.value,[])
    state.selectedParameter.value='fixed';state.addParameter();assert.equal(writes.length,1)
    props.readonly=true;state.selectedParameter.value='sound';state.addParameter();assert.equal(writes.length,1)
  },data=>{
    const value:any=data.ownershipContract().definition.configuration
    value.inputs=[{...value.inputs[0],key:'image',kind:'image',targets:[{target_kind:'workflow_slot',target_key:'image',is_primary:true}]}]
    value.parameters=[{key:'width',value_type:'integer',policy:'overridable',default_value:0}]
    return value
  },'ExecutionConfigurationEditor',data=>({choices:{input_kinds:[],target_kinds:[],parameter_types:[],identity_options:{subject_kinds:[],scope_kinds:[]}},auxiliaryVersions:[],readonly:false,
    options:{input_targets:{image:target('image'),image2:target('image2')},profile_inputs:{theme:{...target('request.theme'),target_kind:'profile_input'}},derived_input_keys:['request.theme'],parameters:data.ownershipContract().definition.interface.parameters}}))
})
