import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {conditioningOptionsFixture,referenceTopologyFixture,referenceModeFixture} from './helpers/referenceTopologyFixture.ts'
const server=()=>createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})

test('topology catalog is required, same-object edits retain refs and full JSON cannot bypass form',async context=>{
  const vite=await server();context.after(()=>vite.close())
  const svc=await vite.ssrLoadModule('/src/services/modelReferenceTopology.ts')
  const parent=await vite.ssrLoadModule('/src/services/modelConditioningContract.ts')
  const options=conditioningOptionsFixture(),mode=referenceModeFixture()
  assert.deepEqual(svc.adaptReferenceTopologyOptions(referenceTopologyFixture()),referenceTopologyFixture())
  for(const bad of [undefined,{}, {...referenceTopologyFixture(),maximum_fps:true},{...referenceTopologyFixture(),preprocessing_slots:[]}]) assert.throws(()=>svc.adaptReferenceTopologyOptions(bad))
  assert.deepEqual(svc.referenceTopologyErrors(mode,options),[])
  const initialized=svc.newReferenceTopology(mode,options.dynamicReferenceTopology)
  assert.equal(initialized.videos.maximum_items,2);assert.deepEqual(initialized.videos.purposes,[]);assert.equal(initialized.preprocessing.videos,null)
  const changed=parent.updateConditioningQuantity(mode,'reference_videos','maximum_items',1)
  assert.equal(changed.dynamic_reference_topology.videos.maximum_items,1)
  assert.equal(changed.dynamic_reference_topology.videos.purposes[0].maximum_items,2)
  assert.ok(svc.referenceTopologyErrors(changed,options).some((error:string)=>error.includes('上限超过')))
  assert.equal(mode.inputs.reference_videos.maximum_items,2)
  const before={conditioning_contract:mode,other:2}
  svc.assertConditioningUnchanged(before,{other:3,conditioning_contract:{...mode}})
  assert.throws(()=>svc.assertConditioningUnchanged(before,{...before,conditioning_contract:changed}),/完整 JSON/)
})

test('purpose cardinality, stale references, pairing and preprocessing fail visibly without destructive repair',async context=>{
  const vite=await server();context.after(()=>vite.close())
  const {referenceTopologyErrors}=await vite.ssrLoadModule('/src/services/modelReferenceTopology.ts')
  const options=conditioningOptionsFixture()
  const cases:[(mode:any)=>void,string][]=[
    [m=>m.dynamic_reference_topology.videos.purposes[0].key='renamed','默认用途'],
    [m=>m.dynamic_reference_topology.pairing_rules[0].source_purpose_keys=[{}],'配对来源'],
    [m=>m.dynamic_reference_topology.pairing_rules.push({...m.dynamic_reference_topology.pairing_rules[0],key:'another'}),'多条配对规则'],
    [m=>m.dynamic_reference_topology.preprocessing.videos.adapter_key='unknown','未注册'],
    [m=>m.dynamic_reference_topology.preprocessing.videos.target_fps=null,'必须填写'],
    [m=>m.dynamic_reference_topology.preprocessing.videos.adapter_key='passthrough_video_v1','不允许'],
    [m=>m.dynamic_reference_topology.preprocessing.videos.minimum_duration_ms=10001,'时长范围'],
    [m=>m.dynamic_reference_topology.mutual_exclusion_rules[0].maximum_distinct_purposes=2,'用途种类'],
    [m=>m.dynamic_reference_topology.pairing_rules[0].maximum_targets_per_source=true,'数量范围'],
    [m=>m.dynamic_reference_topology.audios.default_purpose_key='missing','默认用途'],
  ]
  for(const [mutate,fragment]of cases){const mode=referenceModeFixture();mutate(mode);const before=JSON.stringify(mode);assert.ok(referenceTopologyErrors(mode,options).some((e:string)=>e.includes(fragment)),fragment);assert.equal(JSON.stringify(mode),before)}
})

test('topology component edits original draft, renders orphan choices, respects readonly and missing catalog',async context=>{
  const vite=await server();context.after(()=>vite.close())
  const {createSSRApp,h,reactive}=await import('vue');const {renderToString}=await import('vue/server-renderer')
  const {default:Editor}=await vite.ssrLoadModule('/src/components/ModelReferenceTopologyEditor.vue')
  const props=reactive({mode:referenceModeFixture() as any,options:conditioningOptionsFixture() as any,disabled:false});let state:any,updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:unknown)=>{if(event==='update:modelValue'){props.mode={...props.mode,dynamic_reference_topology:value};updates++}}});return()=>h('div')}}))
  state.groupRow('videos',0,'key','changed');assert.equal(props.mode.dynamic_reference_topology.pairing_rules[0].source_purpose_keys[0],'editing')
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}));assert.match(html,/失效引用：editing/);assert.doesNotMatch(html,/<textarea/)
  state.processValue('videos','target_fps',30);assert.equal(props.mode.dynamic_reference_topology.preprocessing.videos.target_fps,30)
  state.toggle('pairing_rules',0,'source_purpose_keys','changed',true);assert.deepEqual(props.mode.dynamic_reference_topology.pairing_rules[0].source_purpose_keys,['editing','changed'])
  state.toggle('pairing_rules',0,'source_purpose_keys','editing',false);assert.deepEqual(props.mode.dynamic_reference_topology.pairing_rules[0].source_purpose_keys,['changed'])
  props.disabled=true;let before=updates;state.groupRow('videos',0,'label','bad');state.addRule('pairing_rules');state.initialize();assert.equal(updates,before)
  const readonly=await renderToString(createSSRApp({render:()=>h(Editor,props)}));for(const tag of readonly.match(/<(?:input|select|button)\b[^>]*>/g)||[])assert.match(tag,/disabled/)
  props.disabled=false;props.options=null;before=updates;state.addPurpose('videos');assert.equal(updates,before)
})
