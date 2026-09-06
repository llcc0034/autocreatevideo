import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { readFile } from 'node:fs/promises'

test('model quantity editor uses server templates and preserves topology and other fields', async () => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  try {
    const service = await vite.ssrLoadModule('/src/services/modelConditioningContract.ts')
    const slots = ['total_images','first_frame','last_frame','entity_images','reference_videos','paired_video_audios','reference_audios']
    const options = { schemaVersion:1,adapterKey:'media_conditioning_v1',inputKinds:[{value:'classified_references',label:'多参考素材'}],inputSlots:slots.map(value=>({value,label:value})),maximumItems:32,maximumModes:10,identifierPattern:'^[A-Za-z][A-Za-z0-9_.-]{0,63}$',modeTemplate:{key:'',label:'',description:'',input_kind:'',requires_entity_reference_bible:false,supports_native_audio:false,inputs:Object.fromEntries(slots.map(slot=>[slot,{minimum_items:0,maximum_items:0}]))} }
    const mode = service.newConditioningMode(options,'classified_references',[])
    assert.equal(mode.key,'mode_1')
    assert.equal(service.newConditioningMode(options,'classified_references',[mode]).key,'mode_2')
    assert.equal(mode.inputs.total_images.maximum_items,0)
    const contract = {schema_version:1,adapter_key:'media_conditioning_v1',modes:[mode]}
    assert.deepEqual(service.conditioningContractErrors(contract,options),[])
    const changed = service.updateConditioningQuantity(mode,'entity_images','maximum_items',3)
    const changedTotal = service.updateConditioningQuantity(changed,'total_images','maximum_items',3)
    assert.deepEqual(service.conditioningContractErrors({...contract,modes:[changedTotal]},options),[])
    assert.equal(mode.inputs.entity_images.maximum_items,0)
    const topological = {...mode,dynamic_reference_topology:{videos:{minimum_items:0,maximum_items:2,purposes:[{key:'reference',maximum_items:2}]},preprocessing:{videos:{adapter_key:'cfr_video_v1'}}}}
    const narrowed = service.updateConditioningQuantity(topological,'reference_videos','maximum_items',1)
    assert.equal(narrowed.dynamic_reference_topology.videos.maximum_items,1)
    assert.deepEqual(narrowed.dynamic_reference_topology.preprocessing,topological.dynamic_reference_topology.preprocessing)
    assert.equal(narrowed.dynamic_reference_topology.videos.purposes[0].maximum_items,2)
    assert.ok(service.conditioningContractErrors({...contract,modes:[{...mode,key:'invalid key'}]},options).length)
    assert.ok(service.conditioningContractErrors(contract,null).length)
    const excessive = service.updateConditioningQuantity(mode,'entity_images','maximum_items',33)
    assert.ok(service.conditioningContractErrors({...contract,modes:[excessive]},options).length)
    const component = await readFile(new URL('../src/components/RegistryResourceEditor.vue',import.meta.url),'utf8')
    assert.ok(component.includes('ModelConditioningContractEditor'))
    assert.ok(component.includes("update('specification.conditioning_contract',$event)"))
  } finally { await vite.close() }
})

test('numeric range form preserves optional inheritance and rejects malformed values', async () => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  try {
    const service = await vite.ssrLoadModule('/src/services/workflowParameterRanges.ts')
    const {workflowExecutionParameterStep} = await vite.ssrLoadModule('/src/services/workflowExecutionInputs.ts')
    assert.equal(workflowExecutionParameterStep({type:'number',min:0,multipleOf:0.05,alignmentModulus:null}),0.05)
    assert.equal(workflowExecutionParameterStep({type:'number',min:0.03,multipleOf:0.05,alignmentModulus:null}),'any')
    assert.equal(workflowExecutionParameterStep({type:'integer',min:30,multipleOf:1,alignmentModulus:17,alignmentRemainder:5}),'any')
    assert.equal(workflowExecutionParameterStep({type:'integer',min:22,multipleOf:1,alignmentModulus:17,alignmentRemainder:5}),17)
    assert.deepEqual(service.parameterRangeErrors({min:0,max:1,multiple_of:0.05,default:0.3}),[])
    assert.deepEqual(service.updateParameterRange({min:0,max:1},'min',''),{max:1})
    assert.equal(service.updateParameterRange({},'multiple_of','0.05').multiple_of,0.05)
    for (const bad of [{min:2,max:1},{multiple_of:0},{default:3,max:2},{alignment_modulus:17},{min:true},{extra:1}]) assert.ok(service.parameterRangeErrors(bad).length)
    const component = await readFile(new URL('../src/components/WorkflowProjectorDefinitionEditor.vue',import.meta.url),'utf8')
    assert.ok(component.includes('WorkflowParameterRangeEditor'))
    assert.ok(component.includes("delete rows[index].constraints"))
  } finally { await vite.close() }
})

test('model quantity form renders typed controls, edits one draft, and blocks readonly changes', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const oldStorage = Object.getOwnPropertyDescriptor(globalThis,'localStorage')
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=>null}})
  context.after(()=>{if(oldStorage) Object.defineProperty(globalThis,'localStorage',oldStorage); else Reflect.deleteProperty(globalThis,'localStorage')})
  const {createSSRApp,h,reactive} = await import('vue')
  const {renderToString} = await import('vue/server-renderer')
  const {default:Editor} = await vite.ssrLoadModule('/src/components/ModelConditioningContractEditor.vue')
  const slots = ['total_images','first_frame','last_frame','entity_images','reference_videos','paired_video_audios','reference_audios']
  const options = { schemaVersion:1,adapterKey:'media_conditioning_v1',inputKinds:[{value:'classified_references',label:'多参考素材'}],inputSlots:slots.map(value=>({value,label:value})),maximumItems:32,maximumModes:10,identifierPattern:'^[A-Za-z][A-Za-z0-9_.-]{0,63}$',modeTemplate:{key:'',label:'',description:'',input_kind:'',requires_entity_reference_bible:false,supports_native_audio:false,inputs:Object.fromEntries(slots.map(slot=>[slot,{minimum_items:0,maximum_items:0}]))} }
  const props = reactive({modelValue:undefined as any,options,modelProfiles:[],disabled:false})
  let state:any, updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:unknown)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}}});return()=>h('div')}}))
  state.kind.value='classified_references';state.add()
  assert.equal(props.modelValue.modes[0].key,'mode_1')
  state.quantity(0,'entity_images','maximum_items','3')
  state.quantity(0,'total_images','maximum_items','3')
  assert.equal(props.modelValue.modes[0].inputs.total_images.maximum_items,3)
  assert.deepEqual(state.errors.value,[])
  const html = await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/模型素材数量合同/)
  assert.equal((html.match(/type="number"/g)||[]).length,14)
  assert.doesNotMatch(html,/<textarea/)
  props.disabled=true
  const before=updates
  state.quantity(0,'total_images','maximum_items','2');state.add();state.setModes([])
  assert.equal(updates,before)
  const readonly = await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  for(const tag of readonly.match(/<(?:input|select)\b[^>]*>/g)||[]) assert.match(tag,/disabled/)
})
