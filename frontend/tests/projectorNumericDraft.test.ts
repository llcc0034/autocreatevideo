import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import { createServer } from 'vite'
import { readFile } from 'node:fs/promises'

const input = (value: string, badInput = false) => ({ target: { value, validity: { badInput } } })
async function editor(context: TestContext, kind: 'group' | 'classified' | 'range' | 'parent', authoring = false) {
  const vite = await createServer({ logLevel: 'silent', server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  context.after(() => vite.close())
  for (const [key,value] of Object.entries({ localStorage:{getItem:()=>null}, window:{confirm:()=>{throw Error('native confirmation is not allowed')}} })) {
    const old = Object.getOwnPropertyDescriptor(globalThis,key)
    Object.defineProperty(globalThis,key,{configurable:true,value})
    context.after(()=>{if(old)Object.defineProperty(globalThis,key,old);else Reflect.deleteProperty(globalThis,key)})
  }
  const names = {group:'WorkflowAssetGroupConfigEditor',classified:'WorkflowClassifiedReferenceEditor',range:'WorkflowParameterRangeEditor',parent:'WorkflowProjectorDefinitionEditor'}
  const { default: Editor } = await vite.ssrLoadModule(`/src/components/${names[kind]}.vue`)
  const { createRenderer,h,reactive,nextTick,ssrContextKey } = await import('vue')
  const groups = ['images','videos'].map(key=>({key,output_index:2,reducer_output_index:3,wiring:'reduce',minimum_items:0,maximum_items:3}))
  const definition = {asset_slots:groups.map(row=>({key:row.key,contract_source:'workflow'})),projection:{kind:'generic_references',config:{groups,dynamic_groups:Object.fromEntries(groups.map(row=>[row.key,row]))}},parameter_policies:[{policy:'runtime',constraints:{max:3}},{policy:'object_info',constraints:{max:5}}]}
  const props:any = reactive({ modelValue:kind==='range'?{min:0,max:3}:definition, disabled:false,readOnly:false,modelProfiles:[],engineKey:'unavailable',engineVersion:1,
    options:{engines:[],definitionEditor:{sections:['parameters','projection','advanced'].map(key=>({key,label:key,fields:[]})),choices:{assetGroupWirings:[{value:'reduce'}]},limits:{assetSlotsMaxItems:64}}},
  })
  if(authoring){
    props.options.engines=[{key:'unavailable',version:1,projectionFields:[],adapterContract:{}}]
    props.options.definitionEditor.choices.scalarTypes=['null','boolean','string','integer','number'].map(value=>({value}))
    const fields=[['priority','integer'],['detector.node_counts','node_count_map'],['detector.optional_node_counts','node_range_map'],['detector.input_equals','input_equals_list'],['required_edges','edge_list'],['parameter_policies','parameter_policy_list']].map(([path,control])=>({path,control,label:path}))
    props.options.definitionEditor.sections[0].fields=fields
    Object.assign(props.modelValue,{priority:10,detector:{node_counts:{'Node.With.Dot':2},optional_node_counts:{Optional:{min:0,max:3}},input_equals:[{input_name:'first',value:2},{input_name:'second',value:3}]},required_edges:[{output_index:0},{output_index:2}]})
    props.modelValue.parameter_policies=[{policy:'fixed',semantic_key:'seed',value:4},{policy:'fixed',semantic_key:'cfg',value:0.5}]
  }
  let state:any, updates=0, validity:any
  const renderer = createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app = renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='validity')validity=value;else if(event==='update:modelValue'){props.modelValue=value;updates++}}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount());await nextTick()
  return {state,props,nextTick,updates:()=>updates,validity:()=>validity,vite,groups:()=>props.modelValue.projection.config.groups}
}

test('parameter range distinguishes unfinished native input, clear-to-inherit, zero and disabled events', async context=>{
  const e=await editor(context,'range'),s=e.state
  s.numeric('max',input('',true));s.numeric('min',input('Infinity'));await e.nextTick()
  assert.deepEqual(JSON.parse(JSON.stringify(e.props.modelValue)),{min:0,max:3});assert.equal(e.validity().length,2)
  s.numbers.discard('min');await e.nextTick();assert.equal(e.validity().length,1)
  e.props.disabled=true;s.numbers.discard('max');s.numeric('max',input('7'));await e.nextTick();assert.equal(e.updates(),0);assert.equal(e.validity().length,1)
  e.props.disabled=false;s.numeric('max',input(''));await e.nextTick();assert.equal('max' in e.props.modelValue,false);assert.deepEqual(e.validity(),[])
  s.numeric('max',input('0'));await e.nextTick();assert.equal(e.props.modelValue.max,0)
  const before=e.updates();s.numeric('unknown',input('9'));assert.equal(e.updates(),before)
})

test('asset group bounds and both output indices retain originals; drafts survive rename and earlier-row deletion',async context=>{
  const e=await editor(context,'group'),s=e.state,before=JSON.stringify(e.props.modelValue)
  for(const field of ['minimum_items','maximum_items','output_index','reducer_output_index'])s.numeric(1,field,input('',true))
  await e.nextTick();assert.equal(JSON.stringify(e.props.modelValue),before);assert.equal(e.validity().length,4)
  const survivor=s.groupIds.value[1];s.changeSlot(1,'key','renamed');s.remove(0);await e.nextTick()
  assert.equal(s.groupIds.value[0],survivor);assert.equal(e.groups()[0].key,'renamed');assert.equal(e.validity().length,4)
  s.numbers.discard(s.numberKey(0,'output_index'));await e.nextTick();assert.equal(e.validity().length,3)
  s.numeric(0,'maximum_items',input(''));await e.nextTick();assert.equal(e.groups()[0].maximum_items,null)
  s.numeric(0,'minimum_items',input('0'));await e.nextTick();assert.equal(e.groups()[0].minimum_items,0)
})

test('explicit source and wiring changes discard only superseded drafts, and readonly cannot mutate groups',async context=>{
  const e=await editor(context,'group'),s=e.state
  for(const field of ['maximum_items','output_index','reducer_output_index'])s.numeric(0,field,input('',true))
  s.changeSlot(0,'contract_source','model_profile');s.changeSlot(0,'contract_source','workflow');s.change(0,'wiring','ordered_ports');s.change(0,'wiring','reduce');await e.nextTick()
  assert.equal(e.validity().length,1);assert.ok(s.numbers.invalid(s.numberKey(0,'output_index')))
  const before=JSON.stringify(e.props.modelValue),count=e.updates();e.props.readOnly=true
  s.change(0,'output_index',7);s.changeSlot(0,'key','oops');s.remove(0);s.add();s.numeric(0,'output_index',input('4'));s.numbers.discard(s.numberKey(0,'output_index'));await e.nextTick()
  assert.equal(e.updates(),count);assert.equal(JSON.stringify(e.props.modelValue),before);assert.equal(e.validity().length,1)
  e.props.readOnly=false;s.remove(0);s.remove(0);s.add();await e.nextTick();assert.deepEqual(e.validity(),[])
})

test('classified quantity drafts respect source and readonly, and never alter another media group',async context=>{
  const e=await editor(context,'classified'),s=e.state
  s.numeric('videos','maximum_items',input('',true));s.numeric('images','minimum_items',input('',true));await e.nextTick();assert.equal(e.validity().length,2)
  s.source('videos','model_profile');s.source('videos','workflow');await e.nextTick();assert.equal(e.validity().length,1)
  const count=e.updates();e.props.readOnly=true;s.numeric('images','minimum_items',input('2'));s.source('images','model_profile');await e.nextTick();assert.equal(e.updates(),count)
  e.props.readOnly=false;s.numeric('images','minimum_items',input(''));await e.nextTick();assert.equal(e.props.modelValue.projection.config.dynamic_groups.images.minimum_items,null)
  s.numeric('images','minimum_items',input('0'));await e.nextTick();assert.deepEqual(e.validity(),[])
})

test('parent retains visited sections, uses stable parameter identity and aggregates child errors across navigation',async context=>{
  const e=await editor(context,'parent'),s=e.state
  assert.equal(s.retainedSections.value.length,1)
  const id=s.parameterIds.value[1], key=s.parameterErrorKey(1)
  s.childErrors.value[key]=['parameter pending'];await e.nextTick();assert.ok(e.validity().errors.includes('parameter pending'))
  s.selectSection('projection');await e.nextTick();assert.equal(s.retainedSections.value.length,2);assert.ok(e.validity().errors.includes('parameter pending'))
  s.removeListRow('parameter_policies',0);await e.nextTick();assert.equal(s.parameterIds.value[0],id);assert.ok(e.validity().errors.includes('parameter pending'))
  s.changeParameterPolicy('parameter_policies',0,input('fixed'));await e.nextTick();assert.ok(!e.validity().errors.includes('parameter pending'))
  const generation=s.formGeneration.value;s.jsonText.value='{';s.applyJson();await e.nextTick();assert.equal(s.formGeneration.value,generation)
  s.jsonText.value=JSON.stringify(e.props.modelValue);s.applyJson();await e.nextTick();assert.equal(s.formGeneration.value,generation+1);assert.deepEqual(s.childErrors.value,{})
})

test('parent finite drafts survive navigation and unrelated writes; clear is null, zero is explicit',async context=>{
  const e=await editor(context,'parent',true),s=e.state,k=s.numberKey('field','priority')
  s.numeric(k,input('',true),(v:any)=>s.update('priority',v));await e.nextTick()
  assert.equal(e.props.modelValue.priority,10);assert.equal(s.numbers.errors.value.length,1)
  assert.equal(e.validity().pendingEdits,true,'unfinished number must reach the discard guard even though original configuration is unchanged')
  s.selectSection('advanced');s.update('description','changed');await e.nextTick()
  assert.ok(e.validity().errors.some((v:string)=>v.includes('数字尚未完成')))
  s.numeric(k,input(''),(v:any)=>s.update('priority',v));assert.equal(e.props.modelValue.priority,null)
  assert.equal(s.numbers.value(k,null),'');s.numeric(k,input('0'),(v:any)=>s.update('priority',v));assert.equal(e.props.modelValue.priority,0)
  assert.equal(s.numbers.value(k,1.5),1.5);assert.equal(s.numbers.value(k,'broken'),'broken')
  await e.nextTick();assert.equal(e.validity().pendingEdits,false)
  const count=e.updates();s.numeric(s.numberKey('field','unknown'),input('7'),()=>s.update('priority',7));assert.equal(e.updates(),count)
})

test('map rename keeps pending quantities, removing and re-adding does not revive stale errors',async context=>{
  const e=await editor(context,'parent',true),s=e.state,path='detector.node_counts',key=s.numberKey('map',path,'Node.With.Dot','')
  s.numeric(key,input('',true),()=>assert.fail('must not commit'));s.renameMapKey(path,'Node.With.Dot','Renamed.With.Dot');await e.nextTick()
  assert.equal(e.props.modelValue.detector.node_counts['Renamed.With.Dot'],2)
  assert.ok(s.numbers.invalid(s.numberKey('map',path,'Renamed.With.Dot','')))
  s.removeMapRow(path,'Renamed.With.Dot');s.updateMapValue(path,'Renamed.With.Dot',2);await e.nextTick();assert.deepEqual(s.numbers.errors.value,[])
  for(const bound of ['min','max'])s.numeric(s.numberKey('map','detector.optional_node_counts','Optional',bound),input('',true),()=>assert.fail())
  await e.nextTick();assert.equal(s.numbers.errors.value.length,2)
  s.setFallback({target:{checked:true}});assert.equal(s.pendingDiscard.value.kind,'fallback');s.confirmDiscard();await e.nextTick();assert.deepEqual(s.numbers.errors.value,[])
})

test('scalar types keep number choice, parse full exponents and reject integer truncation and implicit null',async context=>{
  const e=await editor(context,'parent',true),s=e.state,path='parameter_policies'
  s.updateListScalar(path,0,4,input('1e2'));assert.equal(e.props.modelValue.parameter_policies[0].value,100)
  for(const raw of ['3.5','9007199254740993','','Infinity']){
    s.updateListScalar(path,0,100,input(raw));await e.nextTick();assert.equal(e.props.modelValue.parameter_policies[0].value,100);assert.ok(s.numbers.invalid(s.listNumberKey(path,0)))
  }
  s.changeListScalarType(path,0,'number');assert.equal(s.scalarKind(path,0,0),'number')
  s.updateListScalar(path,0,0,input('0.5'));assert.equal(e.props.modelValue.parameter_policies[0].value,0.5)
  s.updateListScalar(path,0,0.5,input('0'));assert.equal(s.scalarKind(path,0,0),'number')
  s.updateListScalar(path,0,0,input('-2.5e-2'));assert.equal(e.props.modelValue.parameter_policies[0].value,-0.025)
  s.updateListScalar(path,0,-0.025,input('',true));s.changeListScalarType(path,0,'null');await e.nextTick()
  assert.equal(e.props.modelValue.parameter_policies[0].value,null);assert.deepEqual(s.numbers.errors.value,[])
  const count=e.updates();s.changeListScalarType(path,0,'unknown');assert.equal(e.updates(),count)
})

test('scalar and edge drafts follow surviving local identities, replacement clears only on success',async context=>{
  const e=await editor(context,'parent',true),s=e.state,path='detector.input_equals',edge='required_edges'
  s.updateListScalar(path,1,3,input('',true));s.numeric(s.listNumberKey(edge,1,'output_index'),input('',true),()=>assert.fail())
  const keys=[s.listNumberKey(path,1),s.listNumberKey(edge,1,'output_index')]
  s.removeListRow(path,0);s.removeListRow(edge,0);await e.nextTick();for(const key of keys)assert.ok(s.numbers.invalid(key))
  assert.equal(s.listNumberKey(path,0),keys[0]);assert.equal(s.listNumberKey(edge,0,'output_index'),keys[1])
  s.jsonText.value='{';s.applyJson();assert.equal(s.numbers.errors.value.length,2)
  s.jsonText.value=JSON.stringify(e.props.modelValue);s.applyJson();await e.nextTick();assert.deepEqual(s.numbers.errors.value,[])
  s.updateListScalar('parameter_policies',0,4,input('',true));s.changeParameterPolicy('parameter_policies',0,input('runtime'));s.changeParameterPolicy('parameter_policies',0,input('fixed'));await e.nextTick();assert.deepEqual(s.numbers.errors.value,[])
})

test('readonly and unavailable engine do not consume or commit parent numeric drafts',async context=>{
  const e=await editor(context,'parent',true),s=e.state,key=s.numberKey('field','priority')
  s.numeric(key,input('',true),()=>assert.fail());e.props.readOnly=true
  const before=JSON.stringify(e.props.modelValue),count=e.updates()
  s.numbers.discard(key);s.numeric(key,input('7'),()=>assert.fail());s.changeListScalarType('parameter_policies',0,'number');s.updateListScalar('parameter_policies',0,4,input('9'));s.renameMapKey('detector.node_counts','Node.With.Dot','Oops');s.setFallback({target:{checked:true}})
  await e.nextTick();assert.equal(e.updates(),count);assert.equal(JSON.stringify(e.props.modelValue),before);assert.ok(s.numbers.invalid(key))
  e.props.readOnly=false;e.props.options.engines=[];s.numeric(key,input('8'),()=>assert.fail());assert.ok(s.numbers.invalid(key))
  e.props.engineKey='changed';await e.nextTick();assert.deepEqual(s.numbers.errors.value,[])
})

test('JSON leave confirmation preserves text and numeric drafts on return; approval discards only JSON',async context=>{
  const e=await editor(context,'parent',true),s=e.state,k=s.numberKey('field','priority')
  s.numeric(k,input('',true),()=>assert.fail());s.selectSection('advanced');s.editJson(input('{'))
  await e.nextTick();const before=JSON.stringify(e.props.modelValue),jsonError=s.jsonError.value
  s.selectSection('projection');const pending=s.pendingDiscard.value
  assert.equal(pending.targetKey,'projection');assert.equal(s.activeSectionKey.value,'advanced')
  s.selectSection('parameters');assert.equal(s.pendingDiscard.value,pending,'duplicate does not change target')
  s.pendingDiscard.value=null;assert.equal(s.jsonText.value,'{');assert.equal(s.jsonError.value,jsonError);assert.equal(s.jsonDirty.value,true)
  s.selectSection('projection');s.confirmDiscard();await e.nextTick()
  assert.equal(s.activeSectionKey.value,'projection');assert.equal(s.jsonDirty.value,false)
  assert.equal(s.jsonText.value,JSON.stringify(e.props.modelValue,null,2));assert.equal(s.numbers.invalid(k),true)
  assert.equal(JSON.stringify(e.props.modelValue),before);assert.equal(e.updates(),0)
})

test('fallback confirmation keeps checkbox and detector until approval; unrelated unfinished numbers survive',async context=>{
  const e=await editor(context,'parent',true),s=e.state,k=s.numberKey('field','priority')
  s.numeric(k,input('',true),()=>assert.fail());s.numeric(s.numberKey('map','detector.node_counts','Node.With.Dot',''),input('',true),()=>assert.fail())
  await e.nextTick();const before=JSON.stringify(e.props.modelValue),checkbox={target:{checked:true}}
  s.setFallback(checkbox);assert.equal(checkbox.target.checked,false);assert.equal(e.updates(),0)
  s.pendingDiscard.value=null;assert.equal(JSON.stringify(e.props.modelValue),before);assert.equal(s.numbers.errors.value.length,2)
  checkbox.target.checked=true;s.setFallback(checkbox);s.confirmDiscard();await e.nextTick()
  assert.deepEqual(JSON.parse(JSON.stringify(e.props.modelValue.detector)),{fallback:true,node_counts:{},optional_node_counts:{},input_equals:[]})
  assert.equal(s.numbers.invalid(k),true);assert.equal(s.numbers.errors.value.length,1);assert.equal(e.updates(),1)
  s.confirmDiscard();assert.equal(e.updates(),1)
})

test('pending editor actions expire on content, identity, availability and model replacement changes',async context=>{
  const e=await editor(context,'parent',true),s=e.state
  const changes=[()=>{e.props.disabled=true},()=>{e.props.readOnly=true},()=>{e.props.engineVersion++},
    ()=>{e.props.modelValue=JSON.parse(JSON.stringify(e.props.modelValue))},()=>{e.props.modelValue.priority++},
    ()=>{e.props.projectorVersion={id:'next'}},()=>{e.props.options.definitionEditor.sections[1].label='changed'}]
  for(const change of changes){
    e.props.disabled=false;e.props.readOnly=false;s.selectSection('advanced');s.editJson(input('{'));await e.nextTick()
    s.selectSection('projection');assert.ok(s.pendingDiscard.value);change();s.confirmDiscard();await e.nextTick()
    assert.equal(s.pendingDiscard.value,null);assert.equal(s.activeSectionKey.value,'advanced');assert.equal(s.jsonText.value,'{')
  }
  assert.equal(e.updates(),0)
})

test('field-error location reveals its registered section without applying unfinished JSON or numbers',async context=>{
  const e=await editor(context,'parent',true),s=e.state
  s.selectSection('advanced');s.editJson(input('{'))
  const key=s.numberKey('field','priority');s.numeric(key,input('',true),()=>assert.fail())
  await e.nextTick();const before=JSON.stringify(e.props.modelValue)
  await s.locateIssue('definition.parameter_policies[0].constraints.max')
  assert.equal(s.activeSectionKey.value,'parameters');assert.equal(s.jsonText.value,'{')
  assert.equal(s.jsonDirty.value,true);assert.equal(s.numbers.invalid(key),true)
  assert.equal(JSON.stringify(e.props.modelValue),before);assert.equal(e.updates(),0)
  await s.locateIssue('definition.unregistered.path');assert.equal(s.activeSectionKey.value,'parameters')
})

test('numeric draft validity reaches the original parent gate; tabs retain rather than destroy authoring state',async()=>{
  const parent=await readFile(new URL('../src/components/WorkflowProjectorDefinitionEditor.vue',import.meta.url),'utf8')
  assert.match(parent,/v-for="section in retainedSections" v-show=/)
  assert.doesNotMatch(parent,/window\.confirm/)
  assert.match(parent,/:key="parameterIds\[index\]"/)
  assert.match(parent,/@validity="childErrors\[parameterErrorKey\(index\)\]=\$event"/)
  assert.equal((parent.match(/@validity="childErrors\[projectionErrorKey\(projectionField\)\]=\$event"/g)||[]).length,2)
  for(const name of ['WorkflowParameterRangeEditor','WorkflowAssetGroupConfigEditor','WorkflowClassifiedReferenceEditor']){
    const source=await readFile(new URL(`../src/components/${name}.vue`,import.meta.url),'utf8')
    assert.match(source,/:aria-invalid="numbers.invalid/);assert.match(source,/@click="numbers.discard/)
    assert.doesNotMatch(source,/@input="[^\n]*Number\(text\(\$event\)\)/)
  }
})
