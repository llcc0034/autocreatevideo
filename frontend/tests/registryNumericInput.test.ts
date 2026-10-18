import assert from 'node:assert/strict'
import test, {type TestContext} from 'node:test'
import {readFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {createServer} from 'vite'

async function editor(context:TestContext, resource:string, modelValue:any) {
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  context.after(()=>vite.close())
  const {default:Editor}=await vite.ssrLoadModule('/src/components/RegistryResourceEditor.vue')
  const {createRenderer,h,reactive,nextTick,ssrContextKey}=await import('vue')
  const props=reactive({resource,modelValue,disabled:false,dependencies:{providers:[],modelProfiles:[],deployments:[],runtimeHosts:[],workflowVersions:[],lockedPaths:[] as string[]}})
  let state:any,updates=0,validity:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='update:modelValue'){props.modelValue=value;updates++}else if(event==='validity')validity=value}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount());await nextTick()
  return {state,props,nextTick,updates:()=>updates,validity:()=>validity,field:(path:string)=>state.normalFields.value.find((f:any)=>f.path===path)}
}
const input=(value:string,badInput=false)=>({target:{value,validity:{badInput}}})

test('registry optional number badInput does not silently clear a saved contract',async context=>{
  const e=await editor(context,'model',{modality:'video',specification:{schema_version:1,capabilities:[],execution_monitoring:{schema_version:1,default_idle_timeout_seconds:1800}}})
  const path='specification.execution_monitoring.default_idle_timeout_seconds',field=e.field(path)
  const original=JSON.stringify(e.props.modelValue),before=e.updates()
  e.state.updateNumber(field,input('',true));await e.nextTick()
  assert.equal(JSON.stringify(e.props.modelValue),original);assert.equal(e.updates(),before)
  assert.match(e.validity().errors.join(''),/未完成数字.*原配置保留/)
  assert.equal(e.state.numberValue(field),'')
  e.state.updateText({path:'model_id'},{target:{value:'changed-name'}});await e.nextTick()
  assert.match(e.validity().errors.join(''),/未完成数字/)
  const afterOther=JSON.stringify(e.props.modelValue),updates=e.updates()
  e.props.disabled=true;e.state.updateNumber(field,input('900'));e.state.discardNumberDraft(field);await e.nextTick()
  assert.equal(JSON.stringify(e.props.modelValue),afterOther);assert.equal(e.updates(),updates)
  e.props.disabled=false;e.state.discardNumberDraft(field);await e.nextTick()
  assert.equal(e.state.numberValue(field),'1800');assert.equal(e.updates(),updates)
  assert.equal(e.validity().errors.some((s:string)=>s.includes('未完成数字')),false)
  e.state.updateNumber(field,input(''));await e.nextTick()
  assert.equal(e.props.modelValue.specification.execution_monitoring.default_idle_timeout_seconds,undefined)
})

test('registry numbers distinguish incomplete, finite, zero, decimal and required blank without changing rules',async context=>{
  const e=await editor(context,'deployment',{priority:100}),field=e.field('priority')
  e.state.updateNumber(field,input('',true));await e.nextTick()
  assert.equal(e.props.modelValue.priority,100);assert.equal(e.validity().valid,false)
  e.state.updateNumber(field,input('1e999'));await e.nextTick()
  assert.equal(e.props.modelValue.priority,100);assert.match(e.validity().errors.join(''),/有效有限数字/)
  e.state.updateNumber(field,input('0'));await e.nextTick()
  assert.equal(e.props.modelValue.priority,0);assert.equal(e.state.numberDrafts.priority,undefined)
  e.state.updateNumber(field,input('1.5'));await e.nextTick()
  assert.equal(e.props.modelValue.priority,1.5);assert.match(e.validity().errors.join(''),/路由优先级必须是整数/)
  e.state.updateNumber(field,input(''));await e.nextTick()
  assert.equal(e.props.modelValue.priority,undefined);assert.match(e.validity().errors.join(''),/路由优先级为必填项/)
  e.state.updateNumber(field,input('20'));await e.nextTick()
  assert.equal(e.props.modelValue.priority,20);assert.equal(e.validity().errors.some((s:string)=>s.includes('路由优先级')),false)
})

test('registry number drafts do not leak into a different field type, locked path or resource identity',async context=>{
  const e=await editor(context,'config-definition',{value_type:'integer',is_secret:false,initial_value:12,value_schema:{minimum:0,maximum:20}})
  const initial=e.field('initial_value'),minimum=e.field('value_schema.minimum')
  e.state.updateNumber(initial,input('',true));e.state.updateNumber(minimum,input('',true));await e.nextTick()
  assert.equal(Object.keys(e.state.numberDrafts).length,2)
  e.state.updateNumber(initial,input('15'));await e.nextTick()
  assert.equal(Object.keys(e.state.numberDrafts).length,1)
  e.props.dependencies.lockedPaths=['value_schema.minimum'];await e.nextTick()
  assert.equal(Object.keys(e.state.numberDrafts).length,0)
  const before=e.updates();e.state.updateNumber(minimum,input('9'));assert.equal(e.updates(),before)
  e.state.updateNumber(initial,input('',true));e.props.modelValue={...e.props.modelValue,value_type:'string'};await e.nextTick()
  assert.equal(Object.keys(e.state.numberDrafts).length,0)
  e.state.updateNumber(initial,input('99'));assert.equal(e.props.modelValue.initial_value,15)
  e.props.resource='deployment';e.props.modelValue={id:'one',priority:100};await e.nextTick()
  e.state.updateNumber(e.field('priority'),input('',true));await e.nextTick();assert.equal(Object.keys(e.state.numberDrafts).length,1)
  e.props.modelValue={id:'two',priority:200};await e.nextTick()
  assert.equal(Object.keys(e.state.numberDrafts).length,0);assert.equal(e.state.numberValue(e.field('priority')),'200')
  const source=await readFile(new URL('../src/components/RegistryResourceEditor.vue',import.meta.url),'utf8')
  assert.match(source,/:aria-invalid="!!numberDrafts\[field.path\]"/)
  assert.match(source,/:aria-label="field.label"/)
  assert.match(source,/@click="discardNumberDraft\(field\)"/)
})
