import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createRenderer,createSSRApp,reactive,ssrContextKey,h,nextTick} from 'vue'
import {renderToString} from 'vue/server-renderer'
const root=fileURLToPath(new URL('..',import.meta.url))
async function modules(fn){const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}});try{await fn(await vite.ssrLoadModule('/src/services/recipeStructureMapping.ts'),vite)}finally{await vite.close()}}
const fields=[{selector_key:'fixture.text',label:'隔离目录文本',kind:'text',scope_kind:'shot',revision_kind:'storyboard_approval'},
  {selector_key:'shot.dialogue',label:'已审核对白',kind:'json',scope_kind:'shot',revision_kind:'storyboard_approval',value_schema:{type:'array',items:{type:'object',properties:{text:{type:'string'},speaker_id:{type:'string'}}}}},
  {selector_key:'script.content',label:'剧本',kind:'text',scope_kind:'project',revision_kind:'script_revision'}]
const nodes=[['object','按字段组成对象'],['array','有序列表'],['unit_map','按当前单元分组'],['each','逐条映射列表'],['literal','固定值'],['context_field','受控内容字段'],['selected_output','已采用上游文本'],['unit_key','当前单元标识'],['reference_subjects','参考主体'],['item_field','条目字段']].map(([value,label])=>({value,label}))
function props(changes={}){return {modelValue:{},schema:{},label:'参考内容',nodes,fields,outputs:[],scopes:['shot'],priorStages:['storyboard_review'],disabled:false,...changes}}
function mountSetup(Component,props,emit){let state:any;const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}});const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit});return()=>null}});app.provide(ssrContextKey,{});app.mount({});return {state,unmount:()=>app.unmount()}}

test('initial mapping derives only target fields or immutable const, never source values',()=>modules(async m=>{
  const schema={type:'object',properties:{duration:{type:'integer'},text:{type:'string'}},required:['duration','text']},original=structuredClone(schema)
  assert.deepEqual(m.initializeMapping('object',schema),{kind:'object',fields:{duration:{},text:{}}})
  assert.deepEqual(m.initializeMapping('literal',{type:'integer'}),{kind:'literal',value:null})
  assert.deepEqual(m.initializeMapping('literal',{type:'integer',const:1}),{kind:'literal',value:1})
  assert.deepEqual(m.initializeMapping('unit_map',{}),{kind:'unit_map',value:{}})
  assert.deepEqual(schema,original)
}))
test('controlled leaf menu filters type, scope and preceding approval',()=>modules(async m=>{
  assert.deepEqual(m.mappingSourceChoices(fields,{type:'string'},['shot'],['storyboard_review']),[fields[0],fields[1]])
  assert.deepEqual(m.mappingSourceChoices(fields,{type:'array'},['shot'],['storyboard_review']),[fields[1]])
  assert.deepEqual(m.mappingSourceChoices(fields,{},['shot'],[]),[])
  assert.deepEqual(m.mappingSourceChoices(fields,{},['request'],['storyboard_review']),[])
  const linked={...fields[1],selector_key:'shot.dialogue_with_speakers',revision_dependencies:[{key:'bible',revision_kind:'bible_approval',stage_key:'bible_review'}]}
  assert.deepEqual(m.mappingSourceChoices([linked],{type:'array'},['shot'],['storyboard_review']),[])
  assert.deepEqual(m.mappingSourceChoices([linked],{type:'array'},['shot'],['storyboard_review','bible_review']),[linked])
}))

test('deep source paths and nested each keep declared source schemas separate from target defaults',()=>modules(async m=>{
  const schema={type:'object',properties:{'a.b':{type:'array',items:{type:'object',properties:{'嵌套':{type:'array',items:{type:'object',properties:{'情绪':{type:'string'},flag:{type:'boolean'}}}}}}}}}
  const catalog={fields:[{...fields[1],selector_key:'fixture',value_schema:schema}],outputs:[{outputKey:'fixture',stageKey:'first',kind:'json',scopeKind:'shot',label:'上游',value_schema:schema}]}
  for(const kind of ['context_field','selected_output']) {
    const source=kind==='context_field'?{kind,selector_key:'fixture',path:['a.b']}:{kind,stage_key:'first',output_key:'fixture',path:['a.b']}
    const node={kind:'each',source,item:{kind:'each',source:{kind:'item_field',path:['嵌套']},item:{kind:'item_field',path:['情绪']}}}
    const before=structuredClone(node),target={type:'array',items:{type:'array',items:{type:'string'}}}
    assert.deepEqual(m.mappingErrors(node,target,'mapping',[],catalog),[])
    assert.deepEqual(node,before)
    const bad=structuredClone(node);bad.item.item.path=['missing']
    assert.match(m.mappingErrors(bad,target,'mapping',[],catalog).join(),/未在来源结构/)
    assert.match(m.mappingErrors(node,{type:'array',items:{type:'array',items:{type:'boolean'}}},'mapping',[],catalog).join(),/类型与目标不匹配/)
  }
  for(const path of [null,[-1],[true],['a','b'],['a.b','*']])assert.match(m.mappingErrors({kind:'context_field',selector_key:'fixture',path},{},'mapping',[],catalog).join(),/路径/)
  assert.match(m.mappingErrors({kind:'item_field',field:'情绪'},{},'mapping',[],catalog).join(),/旧field/)
  assert.match(m.mappingErrors({kind:'item_field',path:[]},{},'mapping',[],catalog).join(),/逐条映射/)
  for(const node of [{kind:'context_field',selector_key:'removed'},{kind:'selected_output',output_key:'removed',stage_key:'first',path:[]}])assert.match(m.mappingErrors(node,{},'mapping',[],catalog).join(),/不在当前目录/)
  assert.match(m.mappingErrors({kind:'context_field',selector_key:'fixture',path:['unknown']},{},'mapping',[],{...catalog,fields:[{...fields[1],selector_key:'fixture',value_schema:{}}]}).join(),/未在来源结构/)
}))

test('explicit path repair removes obsolete field and inapplicable fallback without changing readonly values',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const p=reactive(props({itemSchema:{type:'object',properties:{flag:{type:'boolean'}}},modelValue:{kind:'item_field',field:'flag',if_missing:false}})),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>{events.push([event,value]);if(event==='update:modelValue')p.modelValue=value})
  try {
    mounted.state.setPath(['flag']);assert.deepEqual(p.modelValue,{kind:'item_field',path:['flag'],if_missing:false})
    mounted.state.setPath([]);assert.deepEqual(p.modelValue,{kind:'item_field',path:[]})
    p.modelValue={kind:'item_field',path:['flag'],if_missing:false};p.disabled=true;const count=events.length
    mounted.state.setPath([]);assert.equal(events.length,count);assert.equal(p.modelValue.if_missing,false)
  }finally{mounted.unmount()}
}))

test('path control selects literal names and explicit indices, retains invalid paths and honors readonly',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/SchemaPathSelect.vue')).default
  const p=reactive({schema:{type:'object',properties:{'a.b':{type:'array',items:{type:'object',properties:{'情绪':{type:'string'}}}}}},modelValue:[] as unknown,disabled:false}),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>{events.push([event,value]);if(event==='update:modelValue')p.modelValue=value})
  try {
    mounted.state.chooseProperty('a.b');await nextTick();assert.deepEqual(p.modelValue,['a.b'])
    for(const text of ['', '-1','0.5','false','01','9007199254740992']){mounted.state.indexDraft.value=text;mounted.state.chooseIndex();assert.deepEqual(p.modelValue,['a.b'])}
    mounted.state.indexDraft.value='0';mounted.state.chooseIndex();await nextTick();mounted.state.chooseProperty('情绪');await nextTick()
    assert.deepEqual(p.modelValue,['a.b',0,'情绪'])
    p.modelValue=['a.b',0,'missing'];await nextTick()
    const html=await renderToString(createSSRApp({render:()=>h(Component,p)}));assert.match(html,/missing/);assert.match(html,/已保存路径未修改/)
    p.disabled=true;const count=events.length;mounted.state.commit([]);mounted.state.chooseIndex();assert.equal(events.length,count)
    p.disabled=false;mounted.state.commit([]);assert.deepEqual(p.modelValue,[])
  } finally {mounted.unmount()}
}))
test('required nested fields and unresolved list inputs remain explicit errors',()=>modules(async m=>{
  const schema={type:'object',additionalProperties:false,required:['units'],properties:{units:{type:'array',minItems:1,items:{}}}}
  assert.match(m.mappingErrors({kind:'object',fields:{}},schema).join(),/必需/)
  assert.match(m.mappingErrors({kind:'object',fields:{units:{kind:'array',items:[]}}},schema).join(),/至少/)
  assert.match(m.mappingErrors({kind:'each',source:{},item:{kind:'item_field'}},{}).join(),/列表来源/)
  assert.match(m.mappingErrors({kind:'unit_map',value:{}},{}).join(),/当前单元/)
}))

test('only explicit input structures defer missing keys; nested mappings and literal values stay unchanged',()=>modules(async m=>{
  const schema={type:'object',required:['flag','count','text','nullable'],properties:{
    flag:{type:'boolean',default:false},count:{type:'integer',default:0},text:{type:'string',default:''},nullable:{anyOf:[{type:'string'},{type:'null'}],default:null}}}
  const node={kind:'object',fields:{}},before=structuredClone(node)
  assert.equal(m.mappingErrors(node,schema).length,4)
  assert.deepEqual(m.mappingErrors(node,schema,'fields',[schema]),[])
  assert.deepEqual(m.initializeMapping('object',schema,[schema]),node)
  assert.deepEqual(node,before)
  assert.match(m.mappingErrors({kind:'object',fields:{flag:{}}},schema,'fields',[schema]).join(),/尚未选择/)
  assert.ok(m.mappingErrors({kind:'literal',value:{flag:null}},schema,'fields',[schema]).length)
  assert.deepEqual(m.mappingErrors({kind:'literal',value:{flag:false,count:0,text:'',nullable:null}},schema,'fields',[schema]),[])
  const nested={type:'object',required:['rows'],properties:{rows:{type:'array',minItems:1,maxItems:2,items:schema}}}
  assert.ok(m.mappingErrors(node,nested,'fields',[nested]).length)
  assert.deepEqual(m.mappingErrors({kind:'object',fields:{rows:{kind:'array',items:[node]}}},nested,'fields',[nested]),[])
  assert.match(m.mappingErrors({kind:'each',source:{},item:node},nested.properties.rows,'fields',[nested.properties.rows]).join(),/列表来源/)
  assert.deepEqual(m.mappingErrors({kind:'each',source:{kind:'context_field',selector_key:'shot.dialogue'},item:node},nested.properties.rows,'fields',[nested.properties.rows]),[])
  assert.match(m.mappingErrors({kind:'array',items:[node,node,node]},nested.properties.rows,'fields',[nested.properties.rows]).join(),/最多/)
}))

test('closed object offers omit/default and restore mapping without copying a value, including readonly',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const schema={type:'object',additionalProperties:false,required:['flag'],properties:{flag:{type:'boolean',title:'声音开关',default:false}}}
  const p=reactive(props({schema,defaultSchemas:[schema],modelValue:{kind:'object',fields:{flag:{kind:'literal',value:true}}}})),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>{events.push([event,value]);if(event==='update:modelValue')p.modelValue=value})
  try{
    let html=await renderToString(createSSRApp({render:()=>h(Component,p)}))
    assert.match(html,/省略此项，使用结构默认值/)
    mounted.state.removeField('flag');assert.equal(p.modelValue.fields.flag.value,true)
    mounted.state.confirmChange();assert.deepEqual(p.modelValue,{kind:'object',fields:{}})
    html=await renderToString(createSSRApp({render:()=>h(Component,p)}))
    assert.match(html,/已省略/);assert.match(html,/配置 声音开关 的来源/)
    mounted.state.addDeclaredField('flag');assert.deepEqual(p.modelValue.fields.flag,{})
    p.disabled=true;const count=events.length;mounted.state.removeField('flag');assert.equal(events.length,count)
    p.disabled=false;p.defaultSchemas=[];mounted.state.removeField('flag');assert.ok('flag' in p.modelValue.fields)
    html=await renderToString(createSSRApp({render:()=>h(Component,p)}));assert.doesNotMatch(html,/省略此项，使用结构默认值/)
  }finally{mounted.unmount()}
}))

test('actual recipe route validates every target and only the exact structure authorizes missing defaults',()=>modules(async(_,vite)=>{
  const {recipeDefaultContract,recipeDefaultRoute,recipeDefaultOptions}=await vite.ssrLoadModule('/tests/fixtures/recipe-default-data.ts')
  const {executionRegistryApi}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const original=executionRegistryApi.contract
  executionRegistryApi.contract=async(id)=>recipeDefaultContract(id==='fixture-structure')
  const Component=(await vite.ssrLoadModule('/src/components/ProductionRecipeRouteEditor.vue')).default
  const p=reactive({modelValue:recipeDefaultRoute(),options:recipeDefaultOptions,priorStages:[]}),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>events.push([event,value]))
  try{
    await Promise.resolve();await nextTick();await nextTick()
    assert.deepEqual(mounted.state.jsonErrors.value,{})
    assert.deepEqual(events.filter(row=>row[0]==='validity').at(-1)[1],[])
    assert.deepEqual(p.modelValue.input_sources.custom.template.fields,{})
    p.modelValue=recipeDefaultRoute(false);await nextTick();await Promise.resolve();await nextTick()
    assert.match(mounted.state.jsonErrors.value.custom,/必需/)
    p.modelValue=recipeDefaultRoute(true);await nextTick();await Promise.resolve();await nextTick()
    assert.deepEqual(mounted.state.jsonErrors.value,{})
    // The first logical schema is permissive here; the native target remains stricter.
    mounted.state.contract.value.structureSchemas.custom[1].properties.count.maximum=1
    p.modelValue.input_sources.custom.template.fields={count:{kind:'literal',value:2}}
    await nextTick();assert.match(mounted.state.jsonErrors.value.custom,/不能大于/)
    // Changing only the output catalog must invalidate the existing whole-value mapping.
    p.options.textOutputOptions=[{outputKey:'fixture',stageKey:'first',kind:'json',scopeKind:'project',label:'来源',value_schema:{type:'object'}}]
    p.priorStages=[{key:'first'}]
    p.modelValue.input_sources.custom.template={kind:'selected_output',stage_key:'first',output_key:'fixture',scope:{kind:'project'},selection_policy:'reviewed_selected'}
    await nextTick();assert.match(mounted.state.jsonErrors.value.custom,/不在当前目录/)
    mounted.state.contract.value.inputs[0].semantic_contract.allowed_scope_kinds.push('project')
    p.modelValue.input_sources.custom.template={...p.modelValue.input_sources.custom.template}
    await nextTick();assert.deepEqual(mounted.state.jsonErrors.value,{})
    p.options.textOutputOptions=[];await nextTick();assert.match(mounted.state.jsonErrors.value.custom,/不在当前目录/)
    assert.equal(events.filter(row=>row[0]==='update:modelValue').length,0)
  }finally{mounted.unmount();executionRegistryApi.contract=original}
}))

test('nullable object keeps outer constraints and offers omitted fields from the same schema branches',()=>modules(async(m,vite)=>{
  const object={type:'object',required:['flag'],properties:{flag:{type:'boolean',default:false}},additionalProperties:false}
  const nullable={anyOf:[object,{type:'null'}]}
  const raw={kind:'object',fields:{}}
  assert.match(m.mappingErrors(raw,{...object,anyOf:[{type:'object'},{type:'null'}]}).join(),/必需/)
  assert.match(m.mappingErrors(raw,nullable).join(),/必需/)
  assert.deepEqual(m.mappingErrors(raw,nullable,'fields',[nullable]),[])
  assert.ok(m.mappingErrors({kind:'object',fields:{flag:{kind:'literal',value:0}}},nullable,'fields',[nullable]).length)
  assert.deepEqual(m.initializeMapping('object',nullable),{kind:'object',fields:{flag:{}}})
  assert.deepEqual(m.initializeMapping('object',nullable,[nullable]),raw)
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const p=reactive(props({schema:nullable,defaultSchemas:[nullable],modelValue:raw})),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>{events.push([event,value]);if(event==='update:modelValue')p.modelValue=value})
  try{
    const html=await renderToString(createSSRApp({render:()=>h(Component,p)}))
    assert.match(html,/配置 flag 的来源/);assert.doesNotMatch(html,/新增字段名/)
    mounted.state.addDeclaredField('flag');assert.deepEqual(p.modelValue.fields.flag,{})
    p.disabled=true;const count=events.length;mounted.state.addDeclaredField('flag');assert.equal(events.length,count)
  }finally{mounted.unmount()}
}))

test('creation planning uses typed server fields before any generated or reviewed output exists',()=>modules(async(m,vite)=>{
  const planningFields=[['episode_count','本轮计划集数'],['shots_per_episode','本轮每集计划镜数'],
    ['episode_duration_seconds','本轮单集时长预算（秒）']].map(([key,label])=>({
      selector_key:`run.${key}`,label,kind:'json',scope_kind:'project',revision_kind:'production_request',
      value_schema:{type:'integer',minimum:1},description:'读取本轮创建时明确提交并冻结的值。'}))
  const idea={selector_key:'run.idea',label:'本轮创意',kind:'text',scope_kind:'project',revision_kind:'production_request'}
  const catalog=[...fields,idea,...planningFields]
  assert.deepEqual(m.mappingSourceChoices(catalog,{type:'integer'},['project'],[]),planningFields)
  assert.deepEqual(m.mappingSourceChoices(catalog,{type:'number'},['project'],[]),planningFields)
  assert.deepEqual(m.mappingSourceChoices(catalog,{type:'integer'},['shot'],[]),[])
  assert.deepEqual(m.mappingSourceChoices(catalog,{type:'array'},['project'],[]),[])
  const schema={type:'object',additionalProperties:false,properties:Object.fromEntries(planningFields.map(row=>[
    row.selector_key.slice(4),{...row.value_schema,title:row.label}])),required:planningFields.map(row=>row.selector_key.slice(4))}
  const modelValue={kind:'object',fields:Object.fromEntries(planningFields.map(row=>[row.selector_key.slice(4),
    {kind:'context_field',selector_key:row.selector_key,scope:{kind:'project'}}]))}
  assert.deepEqual(m.mappingErrors(modelValue,schema),[])
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const input=props({fields:catalog,scopes:['project'],priorStages:[],schema,modelValue}),before=structuredClone(input)
  const html=await renderToString(createSSRApp({render:()=>h(Component,input)}))
  for(const row of planningFields){assert.ok(html.includes(row.label));assert.ok(html.includes(`value="${row.selector_key}"`))}
  assert.doesNotMatch(html,/<textarea/);assert.doesNotMatch(html,/value="run.idea"|value="fixture.text"/)
  assert.deepEqual(input,before)
  const events=[],p=reactive(props({fields:catalog,scopes:['project'],priorStages:[],schema:{type:'integer'},modelValue:{kind:'context_field'}}))
  const mounted=mountSetup(Component,p,(event,value)=>events.push([event,value]))
  try{
    mounted.state.selectSource('run.episode_count')
    assert.deepEqual(events.at(-1),['update:modelValue',{kind:'context_field',selector_key:'run.episode_count',scope:{kind:'project'}}])
  }finally{mounted.unmount()}
}))

test('selected output mapping only offers declared earlier outputs and uses their exact identity',()=>modules(async(m,vite)=>{
  const output={outputKey:'generated_script.script.project_style',stageKey:'script_generate',kind:'json',scopeKind:'project',label:'已采用剧本项目的当前风格'}
  assert.deepEqual(m.mappingOutputChoices([output],{},['project'],[]),[])
  assert.deepEqual(m.mappingOutputChoices([output],{},['shot'],['script_generate']),[])
  assert.deepEqual(m.mappingOutputChoices([output],{},['project'],['script_generate']),[output])
  assert.match(m.mappingErrors(m.initializeMapping('selected_output',{})).join(),/已采用输出/)
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const p=reactive(props({outputs:[output],scopes:['project'],priorStages:['script_generate','script_review'],modelValue:{kind:'selected_output'}})),events=[]
  const html=await renderToString(createSSRApp({render:()=>h(Component,p)}))
  assert.match(html,/已采用剧本项目的当前风格/);assert.match(html,/不取最新候选/)
  const mounted=mountSetup(Component,p,(event,value)=>events.push([event,value]))
  try{
    mounted.state.selectOutput(m.mappingOutputIdentity(output.stageKey,output.outputKey))
    assert.deepEqual(events.at(-1)[1],{kind:'selected_output',stage_key:'script_generate',output_key:output.outputKey,scope:{kind:'project'},selection_policy:'reviewed_selected'})
    p.disabled=true;const count=events.length;mounted.state.selectOutput(m.mappingOutputIdentity(output.stageKey,output.outputKey));assert.equal(events.length,count)
  }finally{mounted.unmount()}
}))
test('actual recursive form renders target names and source choices without a JSON text area',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const input=props({schema:{type:'object',additionalProperties:false,properties:{narrative:{type:'string',title:'镜头叙事'}},required:['narrative']},
    modelValue:{kind:'object',fields:{narrative:{kind:'context_field',selector_key:'fixture.text',scope:{kind:'shot'}}}}})
  const before=structuredClone(input),html=await renderToString(createSSRApp({render:()=>h(Component,input)}))
  assert.match(html,/镜头叙事/);assert.match(html,/隔离目录文本/)
  assert.doesNotMatch(html,/<textarea/);assert.doesNotMatch(html,/script.content/)
  assert.deepEqual(input,before)
}))
test('real form preserves explicit order, catalog scope and disabled state',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const p=reactive(props({modelValue:{kind:'array',items:[{kind:'literal',value:'first'},{kind:'literal',value:'second'}]}})),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>events.push([event,value]))
  try{
    mounted.state.move(0,1)
    assert.deepEqual(events.at(-1)[1].items.map(row=>row.value),['second','first'])
    assert.equal(p.modelValue.items[0].value,'first')
    mounted.state.selectSource('fixture.text')
    assert.deepEqual(events.at(-1)[1],{kind:'context_field',selector_key:'fixture.text',scope:{kind:'shot'}})
    p.disabled=true;const count=events.length;mounted.state.move(0,1);mounted.state.selectSource('script.content');assert.equal(events.length,count)
  }finally{mounted.unmount()}
}))
test('each form exposes only fields of selected registered list and uses server labels',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const html=await renderToString(createSSRApp({render:()=>h(Component,props({schema:{type:'array',items:{type:'string'}},modelValue:{kind:'each',
    source:{kind:'context_field',selector_key:'shot.dialogue',scope:{kind:'shot'}},item:{kind:'item_field',path:[]}}}))}))
  assert.match(html,/按原顺序读取的列表/);assert.match(html,/speaker_id/);assert.match(html,/条目字段/)
  assert.doesNotMatch(html,/__dict__/)
}))

test('inline kind change restores select immediately and cancel preserves nested false zero and invalid drafts',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const before={kind:'object',fields:{flag:{kind:'literal',value:false},count:{kind:'literal',value:0}}}
  const p=reactive(props({modelValue:structuredClone(before)})),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>{events.push([event,value]);if(event==='update:modelValue')p.modelValue=value})
  const s=mounted.state
  try{
    s.childDrafts.value={'field:count':['未完成数字输入']}
    const select={value:'literal'};s.selectChange({currentTarget:select},'object',s.changeKind)
    assert.equal(select.value,'object');assert.deepEqual(p.modelValue,before);assert.ok(s.pendingChange.value)
    assert.match(events.filter(row=>row[0]==='draftValidity').at(-1)[1].join(),/确认映射/)
    s.child('flag',{kind:'literal',value:true});s.changeKind('array');assert.deepEqual(p.modelValue,before)
    s.keepMapping();assert.deepEqual(p.modelValue,before);assert.deepEqual(s.draftErrors.value,['未完成数字输入'])
    s.changeKind('literal');s.confirmChange();s.confirmChange()
    assert.deepEqual(p.modelValue,{kind:'literal',value:null});assert.equal(events.filter(row=>row[0]==='update:modelValue').length,1)
  }finally{mounted.unmount()}
}))

test('pending mapping becomes stale on external content or catalog change and readonly never applies it',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const p=reactive(props({modelValue:{kind:'literal',value:false}})),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>events.push([event,value])),s=mounted.state
  try{
    s.changeKind('object');p.modelValue={kind:'literal',value:0};s.confirmChange();assert.equal(s.pendingChange.value,null)
    assert.equal(p.modelValue.value,0);assert.match(s.error.value,/已变化/)
    s.changeKind('object');p.nodes=[{value:'literal',label:'固定值'}];s.confirmChange();assert.equal(s.pendingChange.value,null)
    s.changeKind('object');assert.equal(s.pendingChange.value,null)
    p.nodes=nodes;s.changeKind('object');p.disabled=true;s.confirmChange();s.changeKind('array')
    assert.equal(s.pendingChange.value,null);assert.equal(events.filter(row=>row[0]==='update:modelValue').length,0)
  }finally{mounted.unmount()}
}))

test('list-source confirmation retains both source and nested item on cancel and removes only item on accept',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const before={kind:'each',source:{kind:'context_field',selector_key:'shot.dialogue',path:[]},item:{kind:'object',fields:{text:{kind:'item_field',path:['text']},flag:{kind:'literal',value:false}}}}
  const p=reactive(props({modelValue:structuredClone(before)})),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>{events.push([event,value]);if(event==='update:modelValue')p.modelValue=value}),s=mounted.state
  try{
    s.setListSource(before.source);assert.equal(s.pendingChange.value,null)
    const next={kind:'context_field',selector_key:'other',path:['rows']};s.setListSource(next)
    assert.deepEqual(p.modelValue,before);s.keepMapping();assert.deepEqual(p.modelValue,before)
    s.setListSource(next);next.path.push('mutated');s.confirmChange()
    assert.deepEqual(p.modelValue,{...before,source:{kind:'context_field',selector_key:'other',path:['rows']},item:{}})
    assert.equal(events.filter(row=>row[0]==='update:modelValue').length,1)
  }finally{mounted.unmount()}
}))

test('array removal keeps exact index and order and cannot consume stale reordered entries',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const before={kind:'array',items:[{kind:'literal',value:false},{kind:'literal',value:0},{kind:'literal',value:'last'}]}
  const p=reactive(props({modelValue:structuredClone(before)})),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>{events.push([event,value]);if(event==='update:modelValue')p.modelValue=value}),s=mounted.state
  try{
    s.removeItem(1);s.move(0,1);assert.deepEqual(p.modelValue,before);s.keepMapping();assert.deepEqual(p.modelValue,before)
    s.removeItem(1);p.modelValue.items.reverse();s.confirmChange();assert.equal(p.modelValue.items.length,3)
    s.removeItem(1);s.confirmChange();assert.deepEqual(p.modelValue.items,[before.items[2],before.items[0]])
    s.removeItem(99);assert.equal(s.pendingChange.value,null)
  }finally{mounted.unmount()}
}))

test('scope and source path replacement are explicit and rejected path selections reset the DOM choice',()=>modules(async(_,vite)=>{
  const Component=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const p=reactive(props({modelValue:{kind:'path',scope:'root',path:['原字段']},previewContext:{schema:{},scopes:[{value:'root',label:'根'},{value:'item',label:'条目'}]},itemSchema:{},nodes:[{value:'path',label:'路径'}]})),events=[]
  const mounted=mountSetup(Component,p,(event,value)=>{events.push([event,value]);if(event==='update:modelValue')p.modelValue=value}),s=mounted.state
  try{
    const select={value:'item'};s.selectChange({currentTarget:select},'root',s.changeScope);assert.equal(select.value,'root')
    s.keepMapping();assert.deepEqual(p.modelValue.path,['原字段']);s.changeScope('item');s.confirmChange();assert.deepEqual(p.modelValue,{kind:'path',scope:'item'})
    const Path=(await vite.ssrLoadModule('/src/components/SchemaPathSelect.vue')).default
    const path=mountSetup(Path,reactive({schema:{type:'object',properties:{原字段:{type:'string'}}},modelValue:[]}),()=>{})
    try{const control={value:'原字段'};path.state.selectProperty({currentTarget:control});assert.equal(control.value,'')}finally{path.unmount()}
  }finally{mounted.unmount()}
}))
