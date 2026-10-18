import assert from 'node:assert/strict'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const server = () => createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
const quantityCatalog = [
  {value:'entity_images',label:'参考图片',mediaKind:'image'},
  {value:'reference_videos',label:'参考视频',mediaKind:'video'},
]
const quantityProfile = (id='exact-v2') => ({id,specification:{conditioning_contract:{modes:[{key:'Mode',label:'参考模式',inputs:{
  entity_images:{minimum_items:0,maximum_items:6},reference_videos:{minimum_items:0,maximum_items:3},
  unregistered:{minimum_items:0,maximum_items:8},
}}]}}})

test('quantity choices intersect exact model declaration with server media catalog, fail closed and never mutate',async context=>{
  const vite=await server();context.after(()=>vite.close())
  const {modelQuantityChoices:choices}=await vite.ssrLoadModule('/src/services/workflowAssetContractSource.ts')
  const profile=quantityProfile(),before=JSON.stringify(profile)
  assert.equal(choices(profile,'image',quantityCatalog).choices.length,1)
  assert.equal(choices(profile,'image',quantityCatalog).choices[0].inputKey,'entity_images')
  assert.equal(choices(profile,'video',quantityCatalog).choices[0].inputKey,'reference_videos')
  for(const catalog of [undefined,[],[quantityCatalog[0],quantityCatalog[0]],[{...quantityCatalog[0],mediaKind:'unknown'}]]) assert.equal(choices(profile,'image',catalog).choices.length,0)
  for(const value of [0,-1,true,1.5,null]){
    const invalid=quantityProfile();invalid.specification.conditioning_contract.modes[0].inputs.entity_images.maximum_items=value as any
    assert.equal(choices(invalid,'image',quantityCatalog).choices.length,0)
  }
  const duplicate=quantityProfile();duplicate.specification.conditioning_contract.modes.push(duplicate.specification.conditioning_contract.modes[0])
  assert.match(choices(duplicate,'image',quantityCatalog).message,/重复/)
  assert.equal(choices(null,'image',quantityCatalog).choices.length,0)
  assert.equal(choices({specification:{}},'image',quantityCatalog).choices.length,0)
  assert.equal(JSON.stringify(profile),before)
})

test('advanced JSON cannot replace the structured groups or disguise their quantity source',async context=>{
  const vite=await server();context.after(()=>vite.close())
  const {parseWorkflowProjectorJson:parse}=await vite.ssrLoadModule('/src/services/workflowProjectorDefinition.ts')
  const definition={priority:0,asset_slots:[{key:'refs',media_kind:'image',contract_source:'model_profile'}],projection:{kind:'generic_references',config:{groups:[{key:'refs',conditioning_mode:'Mode',model_input:'entity_images'}]}}}
  const before=JSON.stringify(definition)
  for(const mutate of [
    (v:any)=>{v.projection.config.groups[0].model_input='unregistered'},
    (v:any)=>{v.projection.config.groups=[]},
    (v:any)=>{v.asset_slots[0].contract_source='workflow'},
    (v:any)=>{v.projection.kind='generic_fixed'},
  ]){const value=structuredClone(definition);mutate(value);assert.match(parse(JSON.stringify(value),definition).error,/只能在执行引擎表单/)}
  const reordered={...definition,priority:1,projection:{config:definition.projection.config,kind:definition.projection.kind}}
  assert.equal(parse(JSON.stringify(reordered),definition).value.priority,1)
  assert.equal(JSON.stringify(definition),before)
})

test('quantity picker requires explicit application, blocks readonly and stale responses, preserves existing fields',async context=>{
  const vite=await server();context.after(()=>vite.close())
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts'),original=platformApi.getModelProfile
  context.after(()=>{platformApi.getModelProfile=original})
  const pending:any[]=[],events:any[]=[]
  platformApi.getModelProfile=(id:string)=>new Promise(resolve=>pending.push({id,resolve}))
  const {default:Component}=await vite.ssrLoadModule('/src/components/WorkflowAssetContractSource.vue')
  const {createRenderer,h,reactive,nextTick,ssrContextKey}=await import('vue')
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert:()=>{},remove:()=>{},setText:()=>{},setElementText:()=>{},parentNode:()=>null,nextSibling:()=>null,patchProp:()=>{}})
  const props=reactive<any>({slot:{key:'images',media_kind:'image',contract_source:'model_profile'},definition:{},group:{conditioning_mode:'old',model_input:'old_key'},initialModelProfileId:'a',allowQuantitySelection:true,quantityCatalog,readOnly:false,disabled:false})
  let state:any
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose:()=>{},emit:(...args:any[])=>events.push(args)});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});context.after(()=>app.unmount())
  state.expanded.value=true;await nextTick()
  props.initialModelProfileId='b';await nextTick()
  pending[1].resolve(quantityProfile('b'));await Promise.resolve();await nextTick()
  pending[0].resolve(quantityProfile('a'));await Promise.resolve();await nextTick()
  assert.equal(state.profile.value.id,'b');assert.equal(events.length,0)
  state.selectedQuantity.value=state.quantityOptions.value.choices[0].value
  state.applyQuantity();assert.deepEqual(events,[['select-quantity',{conditioning_mode:'Mode',model_input:'entity_images'}]])
  assert.deepEqual(props.group,{conditioning_mode:'old',model_input:'old_key'})
  props.readOnly=true;state.applyQuantity();assert.equal(events.length,1)
  props.readOnly=false;state.selectedQuantity.value=state.quantityOptions.value.choices[0].value
  props.disabled=true;state.applyQuantity();assert.equal(events.length,1)
  props.disabled=false;state.selectedQuantity.value=state.quantityOptions.value.choices[0].value
  props.slot.media_kind='video';state.applyQuantity();assert.equal(events.length,1);assert.equal(state.selectedQuantity.value,'')
  props.group={conditioning_mode:'Mode',model_input:'entity_images'}
  assert.match(state.source.value.message,/媒体类型不匹配/)
  props.initialModelProfileId='c';await nextTick();state.applyQuantity();assert.equal(events.length,1)
  pending[2].resolve(quantityProfile('wrong-id'));await Promise.resolve();await nextTick()
  assert.equal(state.profile.value,null);assert.ok(state.error.value)
  props.workflowContext={state:'loading'};assert.equal(state.canEditQuantity.value,false)
})
const rawSchema = () => ({
  schema_version: 2, available: true, reason_code: '', parameters: [],
  dynamic_reference_modes: {}, dynamic_references: null,
  asset_slots: ['asset.image.source', 'asset.image.source_2', 'asset.image.source_3'].map((key, index) => ({ semantic_key: key, input_slot: index+1, media_kind: 'image', category: 'image', required: index===0, min_items: index===0?1:0, max_items: 1 })),
  asset_groups: [{ key: 'references', media_kind: 'image', min_items: 1, max_items: 3, semantic_keys: ['asset.image.source', 'asset.image.source_2', 'asset.image.source_3'], wiring: 'ordered_ports' }],
})

test('dynamic groups validate shape, exact slot membership, media and cardinality', async (context) => {
  const vite = await server(); context.after(()=>vite.close())
  const {adaptWorkflowInputSchema: adapt} = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const schema = adapt(rawSchema())
  assert.equal(schema.assetGroups[0].maxItems,3)
  for (const mutate of [
    (raw:any)=>{raw.asset_groups[0].max_items=4},
    (raw:any)=>{raw.asset_groups[0].semantic_keys[1]=raw.asset_groups[0].semantic_keys[0]},
    (raw:any)=>{raw.asset_groups[0].media_kind='audio'},
    (raw:any)=>{raw.asset_groups[0].wiring='execute_script'},
    (raw:any)=>{raw.asset_slots[1].required=true},
  ]) { const raw=rawSchema(); mutate(raw); assert.throws(()=>adapt(raw),/动态素材组/) }
})

const inputContract = () => ({
  languageForm: unusedLanguageForm(), versionId: null, sha256: 'a'.repeat(64), operation: 'video.reference_to_video', mode: 'configured', promptMode: 'none', channels: [],
  outputKind: 'video', workflowVersionId: 'workflow', contextFields: [], serverOwnedInputs: [], sourceConstraints: {}, parameters: [],
  inputs: [{key:'references',kind:'image',position:0,required:true,min_items:1,max_items:3,
    semantic_contract:{schema_version:1,allowed_purposes:[],allowed_subject_kinds:[],allowed_scope_kinds:['request'],paired_with_input_key:null,allow_repeated_asset:false},
    targets:[{target_kind:'workflow_slot',target_key:'references',is_primary:true}]}],
})

test('resource input group sends ordered exact Asset uses and rejects missing identities and count overflow', async context => {
  const vite = await server(); context.after(()=>vite.close())
  const m = await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const contract = inputContract()
  for (const count of [1,2,3]) {
    const draft=m.emptyExecutionRequestDraft(contract)
    draft.media.references=Array.from({length:count},(_,n)=>({itemKey:'item-'+n,assetId:'asset-'+n,purpose:null,pairedTo:null,subjectId:null}))
    const fixture=m.buildExecutionRequestFixture(contract,draft)
    assert.deepEqual(fixture.inputs.references.items.map(row=>row.asset_id),Array.from({length:count},(_,n)=>'asset-'+n))
    draft.media.references[0].assetId=''
    assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/明确且唯一/)
  }
  const draft=m.emptyExecutionRequestDraft(contract)
  assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/素材数量/)
  draft.media.references=Array.from({length:4},(_,n)=>({itemKey:'item-'+n,assetId:'asset-'+n,purpose:null,pairedTo:null,subjectId:null}))
  assert.throws(()=>m.buildExecutionRequestFixture(contract,draft),/素材数量/)
})

test('one shared input editor renders resource counts and the formal asset picker without preselecting examples', async context => {
  const vite=await server();context.after(()=>vite.close())
  const {createSSRApp,h}=await import('vue')
  const {renderToString}=await import('vue/server-renderer')
  const {default:Editor}=await vite.ssrLoadModule('/src/components/ExecutionRequestInputEditor.vue')
  const m=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const contract=inputContract(),draft=m.emptyExecutionRequestDraft(contract)
  assert.deepEqual(draft.media.references,[])
  const empty=await renderToString(createSSRApp({render:()=>h(Editor,{contract,modelValue:draft})}))
  assert.match(empty,/数量 1 ～ 3/);assert.match(empty,/添加一项图片/)
  draft.media.references=[{itemKey:'chosen',assetId:'asset-1',purpose:null,pairedTo:null,subjectId:null}]
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{contract,modelValue:draft})}))
  assert.match(html,/选择素材 \/ 上传本地文件/);assert.match(html,/移除此项选择/)
})

test('contract source preview reads the exact declared model path without fallback or mutation', async (context) => {
  const vite=await server();context.after(()=>vite.close())
  const {modelAssetQuantitySource: resolve,assetGroupForSlot}=await vite.ssrLoadModule('/src/services/workflowAssetContractSource.ts')
  const group={key:'refs',conditioning_mode:'Ref2VA',model_input:'total_images'}
  const profile={id:'exact-v2',specification:{conditioning_contract:{schema_version:1,modes:[{key:'Ref2VA',inputs:{total_images:{minimum_items:0,maximum_items:6}}}]}}}
  const before=JSON.stringify(profile)
  assert.match(resolve(null,group).message,/尚未读取/)
  assert.match(resolve({specification:{}},group).message,/未声明 conditioning_contract/)
  assert.match(resolve(profile,{...group,conditioning_mode:'unknown'}).message,/未唯一声明/)
  assert.match(resolve(profile,{...group,model_input:'images'}).message,/未声明数量输入/)
  assert.match(resolve(profile,{...group,model_input:''}).message,/尚未填写/)
  assert.equal(resolve(profile,group).message,'')
  assert.deepEqual(resolve(profile,group).input,{minimum_items:0,maximum_items:6})
  const malformed=structuredClone(profile);malformed.specification.conditioning_contract.modes[0].inputs.total_images.maximum_items=true as any
  assert.match(resolve(malformed,group).message,/缺少有效/)
  assert.equal(JSON.stringify(profile),before)
  assert.equal(assetGroupForSlot({projection:{config:{groups:[group]}}},'refs'),group)
  assert.equal(assetGroupForSlot({projection:{config:{groups:[group,group]}}},'refs'),null)
})
