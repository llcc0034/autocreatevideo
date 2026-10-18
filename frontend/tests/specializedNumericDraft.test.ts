import assert from 'node:assert/strict'
import test, {type TestContext} from 'node:test'
import {readFile} from 'node:fs/promises'
import {createServer} from 'vite'
import {scriptContractFixture} from './helpers/scriptContractFixture.ts'
import {asyncMediaFixture} from './helpers/asyncMediaFixture.ts'

const operation='video.text_to_video'
const input=(value:string,badInput=false)=>({target:{value,validity:{badInput}}})
async function editor(context:TestContext,kind:'script'|'async') {
  const vite=await createServer({logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  context.after(()=>vite.close())
  const script=kind==='script'
  const module=await vite.ssrLoadModule(script?'/src/services/modelScriptContracts.ts':'/src/services/modelAsyncMedia.ts')
  const options=script?module.adaptScriptContracts(scriptContractFixture):module.adaptAsyncMediaOptions(asyncMediaFixture)
  const modelValue=script?{capabilities:options.policies.map((p:any)=>p.operation),...Object.fromEntries(options.policies.map((p:any)=>[p.key,{
    ...Object.fromEntries(p.numberFields.map((f:any)=>[f.key,100])),
    chat_request_options:{temperature:0},
  }]))}:{capabilities:[operation],async_media:{schema_version:1,operations:{[operation]:{output_kind:'video',request_schema:{type:'object',additionalProperties:false,required:['prompt'],properties:{prompt:{type:'string',maxLength:100},width:{type:'integer',minimum:64,maximum:512}}},smoke_inputs:{prompt:'example',width:64}}}}}
  const {default:Editor}=await vite.ssrLoadModule(`/src/components/${script?'ModelScriptContractsEditor':'ModelAsyncMediaEditor'}.vue`)
  const {createRenderer,h,reactive,nextTick,ssrContextKey}=await import('vue')
  const props:any=reactive({modelValue,options,disabled:false,required:true,chatOptions:null})
  let state:any,updates=0,validity:string[]=[]
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='update:modelValue'){props.modelValue=value;updates++}else if(event==='validity')validity=value}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount());await nextTick()
  return {state,props,nextTick,updates:()=>updates,validity:()=>validity}
}

test('script transport numbers keep invalid drafts and reject saving without overwriting the contract',async context=>{
  const e=await editor(context,'script'),s=e.state
  const numberKey=s.numberKey('series_concept','timeout_seconds'),secondKey=s.numberKey('script_structure','timeout_seconds')
  const before=e.updates()
  s.inputNumber('series_concept','timeout_seconds',input('',true))
  s.inputNumber('script_structure','timeout_seconds',input('1e999'))
  await e.nextTick()
  assert.equal(e.updates(),before)
  assert.equal(e.props.modelValue.series_concept.timeout_seconds,100)
  assert.equal(e.props.modelValue.script_structure.timeout_seconds,100)
  assert.equal(s.numbers.errors.value.length,2);assert.equal(e.validity().length,2)
  s.commit('script_structure',{...s.policy('script_structure'),chat_request_options:{temperature:.3}});await e.nextTick()
  assert.equal(s.numbers.invalid(numberKey),true);assert.equal(s.numbers.value(secondKey,100),'1e999')
  s.numbers.discard(numberKey);await e.nextTick()
  assert.equal(s.numbers.invalid(numberKey),false);assert.equal(e.validity().length,1)
  s.inputNumber('script_structure','timeout_seconds',input('77'));await e.nextTick()
  assert.deepEqual(e.validity(),[]);assert.equal(e.props.modelValue.script_structure.timeout_seconds,77)
  s.inputNumber('series_concept','timeout_seconds',input('0'));await e.nextTick()
  assert.equal(e.props.modelValue.series_concept.timeout_seconds,0);assert.ok(e.validity().length,'domain validation is not bypassed')
  s.inputNumber('series_concept','timeout_seconds',input(''));await e.nextTick()
  assert.equal(e.props.modelValue.series_concept.timeout_seconds,undefined);assert.ok(e.validity().length)
})

test('script draft controls fail closed for disabled, missing, hidden and unknown catalog targets',async context=>{
  const e=await editor(context,'script'),s=e.state,id=s.numberKey('series_concept','timeout_seconds')
  s.inputNumber('series_concept','timeout_seconds',input('',true));await e.nextTick()
  const before=e.updates(),options=e.props.options
  e.props.disabled=true;s.inputNumber('series_concept','timeout_seconds',input('20'));s.numbers.discard(id);await e.nextTick()
  assert.equal(s.numbers.invalid(id),true);assert.equal(e.updates(),before)
  e.props.disabled=false;e.props.options=null;s.inputNumber('series_concept','timeout_seconds',input('20'));await e.nextTick()
  assert.equal(e.updates(),before);assert.ok(e.validity().length)
  e.props.options=options;await e.nextTick()
  s.inputNumber('series_concept','unknown',input('20'))
  assert.equal(e.updates(),before)
  s.inputNumber('series_concept','timeout_seconds',input('',true));await e.nextTick()
  const next={...e.props.modelValue,capabilities:[]};delete next.series_concept;e.props.modelValue=next;await e.nextTick()
  assert.equal(s.numbers.invalid(id),false)
  s.inputNumber('series_concept','timeout_seconds',input('20'));assert.equal(e.updates(),before)
  const source=await readFile(new URL('../src/components/ModelScriptContractsEditor.vue',import.meta.url),'utf8')
  assert.match(source,/:aria-invalid="numbers.invalid/);assert.match(source,/@click="numbers.discard/)
})

test('REST schema boundaries preserve invalid input, other edits and explicit zero or absence',async context=>{
  const e=await editor(context,'async'),s=e.state,id=s.boundKey(operation,'width','minimum')
  const before=e.updates()
  s.inputBound(operation,'width','minimum',input('',true));s.inputBound(operation,'prompt','maxLength',input('Infinity'));await e.nextTick()
  assert.equal(e.updates(),before);assert.equal(s.rule(operation,'width').minimum,64);assert.equal(s.rule(operation,'prompt').maxLength,100)
  assert.equal(e.validity().length,2)
  s.setSmoke(operation,'prompt','changed');await e.nextTick()
  assert.equal(s.numbers.invalid(id),true)
  s.numbers.discard(id);s.inputBound(operation,'prompt','maxLength',input('50'));await e.nextTick()
  assert.deepEqual(e.validity(),[]);assert.equal(s.rule(operation,'width').minimum,64)
  s.inputBound(operation,'width','minimum',input('0'));await e.nextTick()
  assert.equal(s.rule(operation,'width').minimum,0);assert.deepEqual(e.validity(),[])
  s.inputBound(operation,'width','minimum',input(''));await e.nextTick()
  assert.equal(Object.hasOwn(s.rule(operation,'width'),'minimum'),false);assert.deepEqual(e.validity(),[])
  s.inputBound(operation,'prompt','maxLength',input(''));await e.nextTick()
  assert.ok(e.validity().length,'required bound remains required')
})

test('REST draft identity follows current schema type and field existence; blocked and stale input cannot write',async context=>{
  const e=await editor(context,'async'),s=e.state,id=s.boundKey(operation,'width','minimum')
  s.inputBound(operation,'width','minimum',input('',true));await e.nextTick()
  const before=e.updates()
  e.props.disabled=true;s.numbers.discard(id);s.inputBound(operation,'width','minimum',input('0'));await e.nextTick()
  assert.equal(e.updates(),before);assert.equal(s.numbers.invalid(id),true)
  e.props.disabled=false;s.editRule(operation,'width','type','number');await e.nextTick()
  assert.equal(s.numbers.invalid(id),false)
  const count=e.updates();s.inputBound(operation,'width','maxLength',input('100'));s.inputBound(operation,'gone','minimum',input('0'))
  assert.equal(e.updates(),count)
  s.inputBound(operation,'width','minimum',input('',true));await e.nextTick()
  const changed=structuredClone(JSON.parse(JSON.stringify(e.props.modelValue)));delete changed.async_media.operations[operation].request_schema.properties.width;e.props.modelValue=changed;await e.nextTick()
  assert.equal(s.numbers.errors.value.length,0)
  s.inputBound(operation,'width','minimum',input('0'));assert.equal(e.updates(),count)
  e.props.options=null;s.inputBound(operation,'prompt','maxLength',input('5'));assert.equal(e.updates(),count)
  const source=await readFile(new URL('../src/components/ModelAsyncMediaEditor.vue',import.meta.url),'utf8')
  assert.match(source,/:aria-invalid="numbers.invalid/);assert.match(source,/@click="numbers.discard/)
})
