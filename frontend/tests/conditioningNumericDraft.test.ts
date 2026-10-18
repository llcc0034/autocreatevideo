import assert from 'node:assert/strict'
import test,{type TestContext} from 'node:test'
import {createServer} from 'vite'
import {readFile} from 'node:fs/promises'
import {conditioningOptionsFixture,referenceModeFixture} from './helpers/referenceTopologyFixture.ts'

const input=(value:string,badInput=false)=>({target:{value,validity:{badInput}}})
async function editor(context:TestContext,kind:'quantity'|'topology') {
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom'})
  context.after(()=>vite.close())
  for(const [key,value] of Object.entries({localStorage:{getItem:()=>null},window:{confirm:()=>true}})){
    const old=Object.getOwnPropertyDescriptor(globalThis,key)
    Object.defineProperty(globalThis,key,{configurable:true,value})
    context.after(()=>{if(old)Object.defineProperty(globalThis,key,old);else Reflect.deleteProperty(globalThis,key)})
  }
  const {default:Editor}=await vite.ssrLoadModule(`/src/components/${kind==='quantity'?'ModelConditioningContractEditor':'ModelReferenceTopologyEditor'}.vue`)
  const {createRenderer,h,reactive,nextTick,ssrContextKey}=await import('vue')
  const mode=referenceModeFixture(),options=conditioningOptionsFixture()
  const props:any=reactive({options,disabled:false,modelProfiles:[],...(kind==='quantity'?{modelValue:{schema_version:1,adapter_key:options.adapterKey,modes:[mode,{...structuredClone(mode),key:'second'}]}}:{mode})})
  let state:any,updates=0,validity:string[]=[]
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='validity')validity=value;else if(event==='update:modelValue'){if(kind==='quantity')props.modelValue=value;else props.mode={...props.mode,dynamic_reference_topology:value};updates++}}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount());await nextTick()
  return {state,props,nextTick,vite,updates:()=>updates,validity:()=>validity,topology:()=>props.mode.dynamic_reference_topology}
}

test('quantity drafts preserve original mirrored bounds and follow modes on deletion',async context=>{
  const e=await editor(context,'quantity'),s=e.state
  const survivingId=s.modeIds.value[1]
  s.quantity(1,'reference_videos','maximum_items',input('',true));await e.nextTick()
  assert.equal(e.props.modelValue.modes[1].inputs.reference_videos.maximum_items,2)
  assert.equal(e.props.modelValue.modes[1].dynamic_reference_topology.videos.maximum_items,2)
  assert.ok(e.validity().length)
  s.change(1,'label','renamed');s.removeMode(0);await e.nextTick()
  assert.equal(s.modeIds.value[0],survivingId)
  assert.equal(s.numbers.invalid(s.quantityKey(0,'reference_videos','maximum_items')),true)
  s.numbers.discard(s.quantityKey(0,'reference_videos','maximum_items'));await e.nextTick()
  assert.deepEqual(e.validity(),[])
  s.quantity(0,'reference_videos','maximum_items',input(''));await e.nextTick()
  assert.equal(e.props.modelValue.modes[0].inputs.reference_videos.maximum_items,null)
  assert.ok(e.validity().length)
})

test('quantity edits reject stale or disabled targets, propagate child validity, and do not carry errors into new modes',async context=>{
  const e=await editor(context,'quantity'),s=e.state,before=e.updates()
  for(const args of [[99,'reference_videos','maximum_items'],[0,'unknown','maximum_items'],[0,'reference_videos','unknown']] as const)s.quantity(...args,input('3'))
  assert.equal(e.updates(),before)
  s.quantity(0,'reference_videos','maximum_items',input('Infinity'));await e.nextTick()
  e.props.disabled=true;s.numbers.discard(s.quantityKey(0,'reference_videos','maximum_items'));s.removeMode(0);s.quantity(0,'reference_videos','maximum_items',input('3'));await e.nextTick()
  assert.equal(e.updates(),before);assert.ok(e.validity().length)
  e.props.disabled=false
  s.topologyErrors.value[s.modeIds.value[1]]=['child pending number'];await e.nextTick();assert.ok(e.validity().includes('child pending number'))
  s.removeMode(1);await e.nextTick();assert.ok(!e.validity().includes('child pending number'))
  s.removeMode(0);s.kind.value='classified_references';s.add();await e.nextTick();assert.deepEqual(e.validity(),[])
  e.props.options=null;const count=e.updates();s.quantity(0,'reference_videos','maximum_items',input('3'));await e.nextTick();assert.equal(e.updates(),count);assert.ok(e.validity().length)
})

test('copying a contract clears drafts only after successful replacement and ignores stale responses',async context=>{
  const e=await editor(context,'quantity'),s=e.state
  const {platformApi}=await e.vite.ssrLoadModule('/src/services/platformApi.ts')
  const original=platformApi.getModelProfile;context.after(()=>platformApi.getModelProfile=original)
  s.copyId.value='source';await e.nextTick()
  s.quantity(0,'reference_videos','maximum_items',input('',true));await e.nextTick()
  platformApi.getModelProfile=async()=>{throw new Error('offline')};await s.copyContract();await e.nextTick();assert.ok(e.validity().length)
  let resolve:any;platformApi.getModelProfile=()=>new Promise(r=>resolve=r)
  const stale=s.copyContract();s.copyId.value='other';await e.nextTick();const count=e.updates()
  resolve({specification:{conditioning_contract:JSON.parse(JSON.stringify({...e.props.modelValue,modes:[]}))}});await stale;assert.equal(e.updates(),count)
  const oldIds=[...s.modeIds.value]
  platformApi.getModelProfile=async()=>({specification:{conditioning_contract:JSON.parse(JSON.stringify(e.props.modelValue))}})
  await s.copyContract();await e.nextTick();assert.deepEqual(e.validity(),[]);assert.notDeepEqual(s.modeIds.value,oldIds)
})

test('all topology numeric families preserve bad inputs independently and expose validity to parent',async context=>{
  const e=await editor(context,'topology'),s=e.state,before=JSON.stringify(e.topology())
  const paths=[['purpose','videos',0,'maximum_items'],['process','videos','target_fps'],['process','videos','minimum_duration_ms'],['rule','pairing_rules',0,'maximum_targets_per_source'],['rule','mutual_exclusion_rules',0,'maximum_distinct_purposes']]
  for(const path of paths)s.numeric(path,input('',true))
  await e.nextTick();assert.equal(JSON.stringify(e.topology()),before);assert.equal(e.validity().length,5)
  s.groupRow('videos',0,'label','changed');await e.nextTick();assert.equal(e.validity().length,5)
  s.numbers.discard(s.numberKey(paths[0]));await e.nextTick();assert.equal(e.validity().length,4)
  s.numeric(paths[1],input('30'));await e.nextTick();assert.equal(e.topology().preprocessing.videos.target_fps,30);assert.equal(e.validity().length,3)
  s.numeric(paths[2],input(''));await e.nextTick();assert.equal(e.topology().preprocessing.videos.minimum_duration_ms,null)
  s.numeric(paths[3],input('0'));await e.nextTick();assert.equal(e.topology().pairing_rules[0].maximum_targets_per_source,0)
})

test('purpose and rule removals keep surviving drafts, while removed preprocessing does not revive old errors',async context=>{
  const e=await editor(context,'topology'),s=e.state
  s.addRule('pairing_rules');s.numeric(['purpose','videos',1,'maximum_items'],input('Infinity'));s.numeric(['rule','pairing_rules',1,'minimum_targets_per_source'],input('',true));await e.nextTick()
  s.removePurpose('videos',0);s.removeRule('pairing_rules',0);await e.nextTick()
  assert.equal(s.numbers.invalid(s.numberKey(['purpose','videos',0,'maximum_items'])),true)
  assert.equal(s.numbers.invalid(s.numberKey(['rule','pairing_rules',0,'minimum_targets_per_source'])),true)
  s.numeric(['process','videos','target_fps'],input('',true));s.removeProcess('videos');s.enableProcess('videos');await e.nextTick()
  assert.equal(s.numbers.invalid(s.numberKey(['process','videos','target_fps'])),false)
  const count=e.updates();e.props.disabled=true;s.numeric(['purpose','videos',0,'maximum_items'],input('1'));s.removePurpose('videos',0);await e.nextTick();assert.equal(e.updates(),count)
  e.props.disabled=false;s.numeric(['purpose','videos',99,'maximum_items'],input('1'));s.numeric(['process','unknown','target_fps'],input('1'));assert.equal(e.updates(),count)
  e.props.options=null;s.numeric(['purpose','videos',0,'maximum_items'],input('1'));assert.equal(e.updates(),count)
})

test('parent wires child validity and stable UI identity; numeric errors have accessible undo controls',async()=>{
  const parent=await readFile(new URL('../src/components/ModelConditioningContractEditor.vue',import.meta.url),'utf8')
  const child=await readFile(new URL('../src/components/ModelReferenceTopologyEditor.vue',import.meta.url),'utf8')
  assert.match(parent,/:key="modeIds\[index\]"/);assert.match(parent,/@validity="topologyErrors\[modeIds\[index\]!\]=\$event"/)
  for(const source of [parent,child]){assert.match(source,/:aria-invalid="numbers.invalid/);assert.match(source,/@click="numbers.discard/)}
  assert.doesNotMatch(child,/const number=\(raw:string\)/)
  assert.match(parent,/label>\.button\{[^}]*height:auto/)
})
