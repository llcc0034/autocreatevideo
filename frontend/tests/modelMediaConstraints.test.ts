import assert from 'node:assert/strict'
import test from 'node:test'
import {readFile} from 'node:fs/promises'
import {createServer} from 'vite'
import {scriptContractFixture} from './helpers/scriptContractFixture.ts'

const rawOptions={schema_version:1,resolution_fields:[
  {key:'width_multiple',label:'宽度倍数',unit:'像素',minimum:1,maximum:2048},
  {key:'minimum_width',label:'最小宽度',unit:'像素',minimum:1,maximum:2048},
  {key:'maximum_width',label:'最大宽度',unit:'像素',minimum:1,maximum:2048},
],resolution_pairs:[['minimum_width','maximum_width']],fps:{minimum:1,maximum:60,maximum_items:3},duration_ms:{minimum:1,maximum:10000}}
const completeOptions={...rawOptions,resolution_fields:[...rawOptions.resolution_fields,
  {key:'height_multiple',label:'高度倍数',unit:'像素',minimum:1,maximum:2048},
  {key:'minimum_height',label:'最小高度',unit:'像素',minimum:1,maximum:2048},
  {key:'maximum_height',label:'最大高度',unit:'像素',minimum:1,maximum:2048},
  {key:'maximum_short_side',label:'短边上限',unit:'像素',minimum:1,maximum:2048},
  {key:'maximum_long_side',label:'长边上限',unit:'像素',minimum:1,maximum:2048},
],resolution_pairs:[...rawOptions.resolution_pairs,['minimum_height','maximum_height']]}
async function setup(context:any){const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}});context.after(()=>vite.close());return vite}

test('media form uses a mandatory bounded catalog, shares one value and rejects unknown or invalid contracts',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelMediaConstraints.ts'),options=m.adaptMediaConstraintsOptions(rawOptions)
  for(const raw of [undefined,{}, {...rawOptions,schema_version:true},{...rawOptions,resolution_fields:[...rawOptions.resolution_fields,rawOptions.resolution_fields[0]]},{...rawOptions,fps:{minimum:1,maximum:60,maximum_items:0}},{...rawOptions,resolution_pairs:[['unknown','maximum_width']]}])assert.throws(()=>m.adaptMediaConstraintsOptions(raw))
  const first=m.updateMediaConstraint(undefined,options,'resolution','width_multiple',32)
  const second=m.updateMediaConstraint(first,options,'duration_ms','minimum',1000)
  assert.equal(first.duration_ms,undefined)
  assert.ok(m.mediaConstraintsErrors(second,options).length)
  const ready=m.updateMediaConstraint(second,options,'duration_ms','maximum',2000)
  assert.deepEqual(m.mediaConstraintsErrors(ready,options),[])
  const cleared=m.updateMediaConstraint(ready,options,'resolution','width_multiple',undefined)
  assert.deepEqual(cleared.resolution,{})
  assert.deepEqual(cleared.duration_ms,{minimum:1000,maximum:2000})
  assert.throws(()=>m.updateMediaConstraint(first,options,'resolution','made_up_key',4))
  assert.throws(()=>m.updateMediaConstraint({schema_version:1,resolution:null},options,'resolution','width_multiple',4))
  for(const raw of [false,{schema_version:true},{schema_version:1,resolution:null},{schema_version:1,custom:4},
    {schema_version:1,resolution:{width_multiple:true}},{schema_version:1,resolution:{minimum_width:1024,maximum_width:352}},
    {schema_version:1,resolution:{width_multiple:2049}},{schema_version:1,fps:{allowed:[24,24]}},
    {schema_version:1,fps:{allowed:[61]}},{schema_version:1,fps:{allowed:[1,2,3,4]}},
    {schema_version:1,duration_ms:{minimum:2000,maximum:1000}},
  ])assert.ok(m.mediaConstraintsErrors(raw,options).length,JSON.stringify(raw))
  assert.deepEqual(m.mediaConstraintsErrors(undefined,options),[])
  assert.ok(m.mediaConstraintsErrors(undefined,null).length)
  m.assertMediaConstraintsUnchanged({media_constraints:ready,other:1},{other:2,media_constraints:{duration_ms:{maximum:2000,minimum:1000},resolution:{width_multiple:32},schema_version:1}})
  assert.throws(()=>m.assertMediaConstraintsUnchanged({media_constraints:ready},{media_constraints:first}),/表单调整/)
})

test('media editor typed controls update the draft, preserve other groups and prevent readonly/catalogless writes',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelMediaConstraints.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/ModelMediaConstraintsEditor.vue')
  const {createSSRApp,h,reactive}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props=reactive({modelValue:undefined as any,options:m.adaptMediaConstraintsOptions(rawOptions),disabled:false})
  let state:any,updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:unknown)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}}});return()=>h('div')}}))
  state.number('resolution','width_multiple','32');state.number('duration_ms','minimum','1000');state.number('duration_ms','maximum','2000')
  // Vue's v-model on type=number produces numbers even without the .number modifier.
  state.fpsDraft.value=24;state.addRate();state.fpsDraft.value='30';state.addRate();state.rate(1,'25')
  assert.deepEqual(props.modelValue.fps.allowed,[24,25]);assert.deepEqual(state.errors.value,[])
  state.removeRate(0);assert.deepEqual(props.modelValue.fps.allowed,[25])
  state.rate(0,'');assert.ok(state.errors.value.length);state.rate(0,'24')
  state.number('resolution','width_multiple','');assert.deepEqual(props.modelValue.resolution,{})
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/尺寸、帧率与时长/);assert.match(html,/最小时长/);assert.match(html,/type="number"/);assert.doesNotMatch(html,/<textarea/)
  props.disabled=true;const before=updates;state.number('resolution','width_multiple','64');state.rate(0,'48');state.addRate();state.removeRate(0);state.clear();assert.equal(updates,before)
  assert.match(await renderToString(createSSRApp({render:()=>h(Editor,props)})),/<fieldset disabled/)
  props.disabled=false;props.options=undefined;state.number('resolution','width_multiple','64');assert.equal(updates,before);assert.ok(state.errors.value.length)
})

test('model registry mounts media form and rejects advanced JSON edits to form-owned media values',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelMediaConstraints.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/RegistryResourceEditor.vue')
  const {createSSRApp,h,reactive}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const {adaptScriptContracts}=await vite.ssrLoadModule('/src/services/modelScriptContracts.ts')
  const options={mediaConstraints:m.adaptMediaConstraintsOptions(rawOptions),scriptContracts:adaptScriptContracts(scriptContractFixture),specificationSchemaVersion:1,providerDrivers:[],modalities:[],artifactRoles:[],capabilities:[],modelFamilies:[]}
  const props=reactive({resource:'model',modelValue:{modality:'video',specification:{schema_version:1,media_constraints:{schema_version:1,resolution:{width_multiple:32}}}} as any,dependencies:{providers:[],modelProfiles:[],deployments:[],runtimeHosts:[],workflowVersions:[],modelProfileOptions:options},disabled:false})
  let state:any
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:unknown)=>{if(event==='update:modelValue')props.modelValue=value}});return()=>h('div')}}))
  const source=JSON.stringify(props.modelValue.specification)
  const change={...props.modelValue.specification,media_constraints:{schema_version:1}}
  state.updateJson({path:'specification',label:'完整模型规格'},{target:{value:JSON.stringify(change)}})
  assert.equal(JSON.stringify(props.modelValue.specification),source);assert.match(state.jsonErrors.specification,/表单调整/)
  state.updateJson({path:'specification',label:'完整模型规格'},{target:{value:JSON.stringify({...props.modelValue.specification,custom:'allowed'})}})
  assert.equal(props.modelValue.specification.custom,'allowed');assert.equal(state.jsonErrors.specification,'')
  const beforeMissingCatalog=JSON.stringify(props.modelValue.specification)
  props.dependencies.modelProfileOptions.scriptContracts=undefined
  state.updateJson({path:'specification',label:'完整模型规格'},{target:{value:JSON.stringify({...props.modelValue.specification,custom:'blocked'})}})
  assert.equal(JSON.stringify(props.modelValue.specification),beforeMissingCatalog)
  assert.match(state.jsonErrors.specification,/传输设置目录未读取/)
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}));assert.match(html,/尺寸、帧率与时长/)
})

test('fixed dimensions are an atomic edit of catalog pairs and preserve all other constraints',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelMediaConstraints.ts'),options=m.adaptMediaConstraintsOptions(completeOptions)
  const before={schema_version:1,resolution:{width_multiple:32,height_multiple:32,minimum_width:32,maximum_width:1024,
    maximum_short_side:352,maximum_long_side:608},fps:{allowed:[24]},duration_ms:{minimum:1000,maximum:2000}}
  const snapshot=JSON.stringify(before)
  const fixed=m.updateFixedResolution(before,options,352,608)
  assert.deepEqual(fixed,{...before,resolution:{...before.resolution,minimum_width:352,maximum_width:352,minimum_height:608,maximum_height:608}})
  assert.equal(JSON.stringify(before),snapshot)
  assert.deepEqual(m.mediaConstraintsErrors(fixed,options),[])
  assert.deepEqual(m.updateFixedResolution(undefined,options,352,608),{schema_version:1,resolution:{minimum_width:352,maximum_width:352,minimum_height:608,maximum_height:608}})
  for(const bad of [undefined,null,0,-1,true,'352',352.5,2049,NaN,Infinity]){
    assert.throws(()=>m.updateFixedResolution(before,options,bad,608))
    assert.throws(()=>m.updateFixedResolution(before,options,352,bad))
    assert.equal(JSON.stringify(before),snapshot)
  }
  for(const bad of [null,m.adaptMediaConstraintsOptions(rawOptions),{...options,resolutionPairs:[]},
    {...options,resolutionFields:options.resolutionFields.map((f:any)=>f.key==='maximum_width'?{...f,minimum:3000}:f)}]){
    assert.equal(m.fixedResolutionFields(bad),null)
  }
  assert.throws(()=>m.updateFixedResolution(before,m.adaptMediaConstraintsOptions(rawOptions),352,608),/目录不完整/)
  const limited={...options,resolutionFields:options.resolutionFields.map((f:any)=>f.key==='maximum_width'?{...f,maximum:512}:f)}
  assert.equal(m.fixedResolutionFields(limited)[0].maximum,512)
  assert.throws(()=>m.updateFixedResolution(before,limited,640,608))
  assert.throws(()=>m.updateFixedResolution({schema_version:1,resolution:null},options,352,608),/格式异常/)
  assert.throws(()=>m.updateFixedResolution({...before,custom:1},options,352,608),/未登记字段/)
  for(const resolution of [{...before.resolution,width_multiple:64},
    {...before.resolution,maximum_short_side:320},{...before.resolution,maximum_long_side:576}]){
    const invalid={...before,resolution},snapshot=JSON.stringify(invalid)
    assert.throws(()=>m.updateFixedResolution(invalid,options,352,608),/倍数|冲突/)
    assert.equal(JSON.stringify(invalid),snapshot)
  }
})

test('resolution authoring checks feasible values, not endpoint divisibility or automatic rounding',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelMediaConstraints.ts'),options=m.adaptMediaConstraintsOptions(completeOptions)
  for(const [resolution,valid] of [
    [{minimum_width:353,maximum_width:400,width_multiple:32},true],
    [{minimum_height:609,maximum_height:640,height_multiple:32},true],
    [{minimum_width:352,minimum_height:608,maximum_short_side:352},true],
    [{maximum_short_side:608,maximum_long_side:352},true],
    [{width_multiple:32},true],
    [{minimum_width:352,maximum_width:352,width_multiple:64},false],
    [{minimum_width:353,maximum_width:383,width_multiple:32},false],
    [{minimum_height:609,maximum_height:639,height_multiple:32},false],
    [{maximum_width:31,width_multiple:32},false],
    [{minimum_width:33,minimum_height:33,width_multiple:32,height_multiple:32,maximum_short_side:63},false],
    [{minimum_width:33,width_multiple:32,maximum_long_side:63},false],
  ] as const){
    const value={schema_version:1,resolution},snapshot=JSON.stringify(value)
    assert.equal(m.mediaConstraintsErrors(value,options).length===0,valid,snapshot)
    assert.equal(JSON.stringify(value),snapshot)
  }
})

test('fixed dimension shortcut keeps unequal ranges until explicit apply, blocks stale or readonly edits',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelMediaConstraints.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/ModelMediaConstraintsEditor.vue')
  const {createRenderer,createSSRApp,h,reactive,nextTick,ssrContextKey}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const original={schema_version:1,resolution:{minimum_width:32,maximum_width:1024,minimum_height:32,maximum_height:1024,width_multiple:32,height_multiple:32},fps:{allowed:[24]}}
  const props=reactive({modelValue:structuredClone(original) as any,options:m.adaptMediaConstraintsOptions(completeOptions),disabled:false})
  let state:any,updates=0,validity:string[]=[]
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:any)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}else if(event==='validity')validity=value}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount())
  assert.equal(updates,0);state.startFixed();await nextTick()
  assert.deepEqual(state.fixedDraft.value,{width:'',height:''});assert.deepEqual(props.modelValue,original)
  assert.match(validity.join(''),/尚未应用/)
  state.number('resolution','minimum_width','10');assert.equal(updates,0)
  state.fixedDraft.value.width='352';state.applyFixed();assert.equal(updates,0);assert.match(state.errors.value.join(''),/固定高度/)
  state.fixedDraft.value.height='608';state.cancelFixed();await nextTick();assert.equal(updates,0);assert.deepEqual(props.modelValue,original);assert.deepEqual(validity,[])
  state.startFixed();state.fixedDraft.value.width='352';state.fixedDraft.value.height='608';state.applyFixed();await nextTick()
  assert.equal(updates,1);assert.equal(state.fixedDraft.value,null);assert.deepEqual(validity,[])
  assert.equal(props.modelValue.resolution.maximum_width,352);assert.equal(props.modelValue.resolution.minimum_height,608);assert.deepEqual(props.modelValue.fps,original.fps)
  state.number('resolution','width_multiple','64');await nextTick()
  assert.match(validity.join(''),/宽度范围内没有满足倍数/)
  assert.equal(props.modelValue.resolution.minimum_width,352) // No automatic rounding.
  state.startFixed();state.applyFixed();await nextTick()
  assert.match(validity.join(''),/宽度范围内没有满足倍数/);assert.notEqual(state.fixedDraft.value,null)
  assert.equal(updates,2);state.cancelFixed();state.number('resolution','width_multiple','32');await nextTick()
  assert.deepEqual(validity,[]);assert.equal(updates,3)
  state.startFixed();assert.deepEqual(state.fixedDraft.value,{width:'352',height:'608'})
  props.modelValue=structuredClone(original);await nextTick();assert.equal(state.fixedDraft.value,null);assert.match(state.errors.value.join(''),/已变化/)
  state.startFixed();props.disabled=true;state.applyFixed();state.cancelFixed();assert.equal(updates,3)
  props.disabled=false;state.cancelFixed();props.options=undefined;state.startFixed();assert.equal(state.fixedDraft.value,null);assert.equal(updates,3)
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{...props,options:m.adaptMediaConstraintsOptions(completeOptions)})}))
  assert.match(html,/固定宽高快捷设置/);assert.match(html,/设置固定宽高/);assert.match(html,/不是本次生成尺寸/)
})

test('incomplete browser numbers preserve media constraints and block saving until repaired or discarded',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelMediaConstraints.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/ModelMediaConstraintsEditor.vue')
  const {createRenderer,h,reactive,nextTick,ssrContextKey}=await import('vue')
  const original={schema_version:1,resolution:{width_multiple:32,height_multiple:32},fps:{allowed:[24,30,48]},duration_ms:{minimum:1000,maximum:2000}}
  const props=reactive({modelValue:structuredClone(original) as any,options:m.adaptMediaConstraintsOptions(completeOptions),disabled:false})
  let state:any,updates=0,validity:string[]=[]
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:any)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}else if(event==='validity')validity=value}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount())
  const input=(value:string,badInput=false)=>({target:{value,validity:{badInput}}})
  state.numberInput('resolution','width_multiple','宽度倍数',input('',true))
  state.numberInput('duration_ms','minimum','最小时长',input('',true))
  state.rateInput(2,input('',true));await nextTick()
  assert.equal(updates,0);assert.deepEqual(props.modelValue,original)
  assert.equal(validity.length,3);assert.match(validity.join(''),/原配置保留/)
  assert.equal(state.numericValue('resolution','width_multiple',32),'')
  state.startFixed();assert.equal(state.fixedDraft.value,null)
  state.numberInput('resolution','height_multiple','高度倍数',input('16'));await nextTick()
  assert.equal(updates,1);assert.equal(validity.length,3) // Another valid edit cannot hide incomplete inputs.
  state.numberInput('resolution','width_multiple','宽度倍数',input('64'));await nextTick()
  assert.equal(validity.length,2);assert.equal(props.modelValue.resolution.width_multiple,64)
  state.removeRate(0);await nextTick()
  assert.deepEqual(props.modelValue.fps.allowed,[30,48]);assert.equal(validity.length,2)
  assert.equal(state.numericValue('fps','1',48),'');assert.match(validity.join(''),/帧率 2/)
  assert.equal(state.numericDrafts.value['fps.2'],undefined)
  state.rateInput(1,input('60'));await nextTick();assert.equal(validity.length,1)
  assert.deepEqual(props.modelValue.fps.allowed,[30,60])
  const beforeDiscard=JSON.stringify(props.modelValue),beforeUpdates=updates
  props.disabled=true;state.discardIncompleteNumbers();state.numberInput('duration_ms','minimum','最小时长',input('1200'));state.removeRate(0);await nextTick()
  assert.equal(JSON.stringify(props.modelValue),beforeDiscard);assert.equal(validity.length,1);assert.equal(updates,beforeUpdates)
  props.disabled=false;props.options=undefined;state.discardIncompleteNumbers();await nextTick()
  assert.equal(state.incompleteNumbers.value.length,1);assert.match(validity.join(''),/目录未读取/)
  props.options=m.adaptMediaConstraintsOptions(completeOptions);state.discardIncompleteNumbers();await nextTick()
  assert.deepEqual(validity,[]);assert.equal(JSON.stringify(props.modelValue),beforeDiscard);assert.equal(updates,beforeUpdates)
  state.numberInput('resolution','width_multiple','宽度倍数',input(''));await nextTick()
  assert.equal('width_multiple' in props.modelValue.resolution,false) // A deliberate valid blank still clears this field.
  state.rateInput(0,input('',true));await nextTick();assert.equal(validity.length,1)
  state.removeRate(0);await nextTick();assert.deepEqual(validity,[]);assert.deepEqual(props.modelValue.fps.allowed,[60])
  state.startFixed();state.numberInput('resolution','height_multiple','高度倍数',input('',true))
  assert.equal(state.incompleteNumbers.value.length,0);state.cancelFixed()
  const source=await readFile(new URL('../src/components/ModelMediaConstraintsEditor.vue',import.meta.url),'utf8')
  assert.match(source,/input\.validity\.badInput/)
  assert.match(source,/@input="numberInput\('resolution',field.key,field.label,\$event\)"/)
  assert.match(source,/@input="rateInput\(index,\$event\)"/)
  assert.match(source,/@input="numberInput\('duration_ms',field.key,field.label,\$event\)"/)
  assert.match(source,/aria-invalid/)
})
