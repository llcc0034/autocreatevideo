import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createRenderer, reactive, ssrContextKey, nextTick } from 'vue'
let versionA, versionB, definitionB, sourceSchema, sourceSchemaB, targetSchema, leaf, structures, route, contract, options

async function modules(fn){const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',logLevel:'silent'});try{({versionA,versionB,definitionB,sourceSchema,sourceSchemaB,targetSchema,leaf,structures,route,contract,options}=await vite.ssrLoadModule('/tests/fixtures/shot-field-mapping-data.ts'));await fn(vite)}finally{await vite.close()}}
function mountSetup(Component,props,emit=()=>{}){let state:any;const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}});const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit});return()=>null}});app.provide(ssrContextKey,{});app.mount({});return {state,unmount:()=>app.unmount()}}
const settle=async()=>{await Promise.resolve();await nextTick();await Promise.resolve();await nextTick()}
function mocks(api){api.get=async id=>structures.find(row=>row.id===id);api.schema=async id=>({valid:true,json_schema:id===versionA?sourceSchema:sourceSchemaB,errors:{}})}

test('retired fixed creative sources remain invalid without rewriting saved routes; exact fields recover explicitly',()=>modules(async vite=>{
  const {dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts');mocks(api)
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts');executionRegistryApi.contract=async()=>contract()
  const Component=(await vite.ssrLoadModule('/src/components/ProductionRecipeRouteEditor.vue')).default
  const p=reactive({modelValue:route(),options,stageKey:'media_generation',priorStages:[{key:'storyboard_review'}]}),events:any[]=[]
  const mounted=mountSetup(Component,p,(...event)=>events.push(event))
  try{
    for(const key of ['content','narrative','detailed_description','action','camera','sound_effects','music','continuity_notes']){
      p.modelValue=route();p.modelValue.input_sources.custom.template.fields.tone={kind:'context_field',selector_key:'shot.'+key,scope:{kind:'shot'}}
      const original=JSON.stringify(p.modelValue);await settle()
      assert.match(mounted.state.jsonErrors.value.custom,/来源已不在当前目录/)
      assert.equal(JSON.stringify(p.modelValue),original)
      assert.equal(events.filter(row=>row[0]==='update:modelValue').length,0)
    }
    p.modelValue=route();await settle();assert.deepEqual(mounted.state.jsonErrors.value,{})
  }finally{mounted.unmount()}
}))

test('exact field identity and nullable type/path navigation reuse server schema; duplicates and mismatches fail',()=>modules(async vite=>{
  const m=await vite.ssrLoadModule('/src/services/recipeStructureMapping.ts')
  const s=await vite.ssrLoadModule('/src/services/shotFieldMapping.ts')
  const catalog={fields:[],outputs:[],shotFields:{[versionA]:{loading:false,error:'',schema:sourceSchema}}}
  const before=JSON.stringify(route().input_sources)
  assert.deepEqual(m.mappingErrors(route().input_sources.custom.template,targetSchema,'value',[],catalog),[])
  assert.equal(JSON.stringify(route().input_sources),before)
  assert.equal(s.shotFieldOptions(sourceSchema).find(row=>row.nodeKey==='tone').label,'情绪描述 · v1 · $["情绪.原文"]')
  assert.match(m.mappingErrors({...leaf(),field:{...leaf().field,field_definition_version_id:definitionB}},{},'value',[],catalog).join(),/字段版本/)
  assert.match(m.mappingErrors({...leaf(),path:['missing']},{},'value',[],catalog).join(),/路径/)
  assert.match(m.mappingErrors(leaf(),{type:'boolean'},'value',[],catalog).join(),/类型与目标/)
  const duplicate=structuredClone(sourceSchema);duplicate.properties.copy=duplicate.properties['情绪.原文']
  assert.match(s.shotFieldSchema(leaf(),{[versionA]:{loading:false,error:'',schema:duplicate}}).error,/节点/)
  const nullable=structuredClone(sourceSchema);nullable.properties['情绪.原文']={anyOf:[sourceSchema.properties['情绪.原文'],{type:'null'}]}
  assert.deepEqual(s.shotFieldSchema(leaf(),{[versionA]:{loading:false,error:'',schema:nullable}}).schema,nullable.properties['情绪.原文'])
  assert.deepEqual(s.shotFieldNodes({kind:'literal',value:leaf()}),[])
  assert.match(s.shotFieldSchema(leaf()).error,/正在读取/)
}))

test('route loads each selected schema once, blocks loading/errors and preserves saved mappings through recovery',()=>modules(async vite=>{
  const {dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts');mocks(api)
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts');executionRegistryApi.contract=async()=>contract()
  const Component=(await vite.ssrLoadModule('/src/components/ProductionRecipeRouteEditor.vue')).default
  let readCount=0, finish:any;api.schema=async()=>{readCount++;return await new Promise(resolve=>{finish=resolve})}
  const p=reactive({modelValue:route(),options,stageKey:'media_generation',priorStages:[{key:'storyboard_review'}]}),events:any[]=[]
  const mounted=mountSetup(Component,p,(...event)=>events.push(event)),before=JSON.stringify(p.modelValue)
  try{
    await settle();assert.match(mounted.state.jsonErrors.value.custom,/正在读取/);assert.equal(readCount,1)
    finish({valid:true,json_schema:sourceSchema,errors:{}});await settle()
    assert.deepEqual(mounted.state.jsonErrors.value,{})
    p.priorStages=[];await settle();assert.match(mounted.state.sourceErrors.value.join(),/前置分镜审核/)
    p.priorStages=[{key:'storyboard_review'}];await settle();assert.deepEqual(mounted.state.sourceErrors.value,[])
    p.stageKey='bible_image_generation';await settle();assert.match(mounted.state.sourceErrors.value.join(),/媒体步骤/)
    assert.ok(!mounted.state.structureNodes.value.some(row=>row.value==='shot_field'))
    p.stageKey='media_generation';await settle()
    api.schema=async()=>{throw Error('目录读取断开')};await mounted.state.shotCatalog.load(versionA);await settle()
    assert.match(mounted.state.jsonErrors.value.custom,/断开/);assert.equal(mounted.state.shotCatalog.states[versionA].schema,undefined)
    mocks(api);await mounted.state.shotCatalog.load(versionA);await settle();assert.deepEqual(mounted.state.jsonErrors.value,{})
    assert.equal(JSON.stringify(p.modelValue),before);assert.equal(events.filter(row=>row[0]==='update:modelValue').length,0)
  }finally{mounted.unmount()}
}))

test('switching exact versions/unmount discards late schema results; failed status cannot become ready',()=>modules(async vite=>{
  const {dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts');mocks(api)
  const Component=(await vite.ssrLoadModule('/src/components/ShotFieldMappingSelect.vue')).default
  let finish:any;api.schema=async id=>id===versionA?await new Promise(resolve=>{finish=resolve}):{valid:true,json_schema:sourceSchemaB,errors:{}}
  const p=reactive({modelValue:leaf(),disabled:false}),mounted=mountSetup(Component,p)
  await settle();p.modelValue=leaf('other-tone',versionB);await settle()
  finish({valid:true,json_schema:sourceSchema,errors:{}});await settle()
  assert.equal(mounted.state.catalog.states[versionA],undefined);assert.equal(mounted.state.selected.value.error,'')
  api.get=async()=>({...structures[1],status:'draft'});await mounted.state.catalog.load(versionB);assert.match(mounted.state.state.value.error,/草稿/)
  mocks(api);let last:any;api.schema=async()=>await new Promise(resolve=>{last=resolve})
  const pending=mounted.state.catalog.load(versionB);mounted.unmount();last({valid:true,json_schema:sourceSchemaB,errors:{}});await pending
  assert.equal(mounted.state.state.value.schema,undefined)
}))

test('picker preserves identity, explicit changes clear only dependent path, cancellation and readonly do not write',()=>modules(async vite=>{
  const {dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts');mocks(api)
  const Picker=(await vite.ssrLoadModule('/src/components/ShotFieldMappingSelect.vue')).default
  const Editor=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const p=reactive({modelValue:{...leaf('movement'),path:['lines']},disabled:false}),events:any[]=[]
  const savedWindow=globalThis.window;globalThis.window={confirm:()=>false} as any
  const picker=mountSetup(Picker,p,(event,value)=>{events.push([event,value]);if(event==='update:modelValue')p.modelValue=value})
  try{
    await settle();picker.state.changeVersion(versionB);assert.equal(events.length,0)
    picker.state.changeField('tone');assert.deepEqual(p.modelValue,leaf())
    p.disabled=true;picker.state.changeField('movement');picker.state.changeVersion(versionB);assert.equal(events.length,1)
    p.disabled=false;globalThis.window.confirm=()=>true;picker.state.changeVersion(versionB);await settle()
    assert.equal(p.modelValue.field.structure_version_id,versionB);assert.equal(p.modelValue.field.node_key,'');assert.equal(p.modelValue.path,undefined)
    assert.match(picker.state.selected.value.error,/请选择/)
    picker.state.changeField('other-tone');assert.equal(p.modelValue.field.field_definition_version_id,definitionB)
    const ep=reactive({modelValue:{...leaf('movement'),path:[]},schema:{},label:'路径',nodes:options.structureNodeOptions,fields:[],outputs:[],scopes:['shot'],priorStages:['storyboard_review']})
    const editor=mountSetup(Editor,ep,(event,value)=>{if(event==='update:modelValue')ep.modelValue=value})
    try{await settle();editor.state.setPath(['lines']);assert.deepEqual(ep.modelValue.field,leaf('movement').field);assert.deepEqual(ep.modelValue.path,['lines'])}finally{editor.unmount()}
  }finally{picker.unmount();globalThis.window=savedWindow}
}))

test('native selectors return to accepted values when parent cancels a dependent mapping change',()=>modules(async vite=>{
  const {dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts');mocks(api)
  const Version=(await vite.ssrLoadModule('/src/components/DataStructureVersionSelect.vue')).default
  const Picker=(await vite.ssrLoadModule('/src/components/ShotFieldMappingSelect.vue')).default
  const vp=reactive({modelValue:versionA,disabled:false}),events:any[]=[]
  let accept=false
  const version=mountSetup(Version,vp,(event,value)=>{events.push([event,value]);if(accept)vp.modelValue=value})
  const field=mountSetup(Picker,reactive({modelValue:leaf(),disabled:false}))
  try{
    await settle()
    const target={value:versionB};await version.state.chooseVersion({target});assert.equal(target.value,versionA)
    accept=true;target.value=versionB;await version.state.chooseVersion({target});assert.equal(target.value,versionB)
    vp.disabled=true;target.value=versionA;await version.state.chooseVersion({target});assert.equal(target.value,versionB);assert.equal(events.length,2)
    const fieldTarget={value:'movement'};await field.state.chooseField({target:fieldTarget});assert.equal(fieldTarget.value,'tone')
  }finally{version.unmount();field.unmount()}
}))
