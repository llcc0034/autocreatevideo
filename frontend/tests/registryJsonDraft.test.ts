import assert from 'node:assert/strict'
import test, {type TestContext} from 'node:test'
import {readFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {createServer} from 'vite'

async function editor(context:TestContext,resource:string,modelValue:any) {
  const vite=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  context.after(()=>vite.close())
  const {default:Editor}=await vite.ssrLoadModule('/src/components/RegistryResourceEditor.vue')
  const {createRenderer,h,reactive,nextTick,ssrContextKey,createSSRApp}=await import('vue')
  const props=reactive({resource,modelValue,disabled:false,dependencies:{providers:[],modelProfiles:[],deployments:[],runtimeHosts:[],workflowVersions:[],lockedPaths:[] as string[]}})
  let state:any,updates=0,validity:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='update:modelValue'){props.modelValue=value;updates++}else if(event==='validity')validity=value}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount());await nextTick()
  const {renderToString}=await import('vue/server-renderer')
  return {state,props,nextTick,updates:()=>updates,validity:()=>validity,field:(path:string)=>state.advancedFields.value.find((f:any)=>f.path===path),html:()=>renderToString(createSSRApp({render:()=>h(Editor,props)}))}
}
const input=(value:string)=>({target:{value}})

test('invalid JSON and its error survive unrelated form changes until explicit correction',async context=>{
  const e=await editor(context,'config-definition',{value_type:'integer',value_schema:{minimum:0},initial_value:3})
  const field=e.field('value_schema'),before=e.updates()
  e.state.updateJson(field,input('{"minimum":'));await e.nextTick()
  assert.equal(e.updates(),before);assert.deepEqual(e.props.modelValue.value_schema,{minimum:0})
  e.state.updateText({path:'label'},input('changed label'));await e.nextTick()
  assert.equal(e.state.jsonDrafts.value_schema,'{"minimum":')
  assert.match(e.state.jsonErrors.value_schema,/不是合法 JSON/)
  assert.equal(e.validity().valid,false)
  e.state.updateJson(field,input('{"minimum":0,"maximum":5}'));await e.nextTick()
  assert.deepEqual(e.props.modelValue.value_schema,{minimum:0,maximum:5})
  assert.equal(e.state.jsonErrors.value_schema,'')
})

test('stale JSON cannot overwrite a typed edit of the same object and explicit reload keeps that edit',async context=>{
  const e=await editor(context,'config-definition',{value_type:'integer',value_schema:{minimum:0,maximum:9},initial_value:3})
  const field=e.field('value_schema')
  e.state.updateJson(field,input('{'));await e.nextTick()
  e.state.update('value_schema.minimum',2);await e.nextTick()
  assert.equal(e.state.jsonDrafts.value_schema,'{')
  assert.match(e.state.jsonErrors.value_schema,/同一配置已.*修改/)
  const before=e.updates()
  e.state.updateJson(field,input('{"minimum":0,"maximum":9}'));await e.nextTick()
  assert.equal(e.updates(),before);assert.equal(e.props.modelValue.value_schema.minimum,2)
  e.props.disabled=true;e.state.discardJsonDraft(field);e.state.updateJson(field,input('{}'));await e.nextTick()
  assert.match(e.state.jsonErrors.value_schema,/同一配置已/);assert.equal(e.updates(),before)
  e.props.disabled=false;e.state.discardJsonDraft(field);await e.nextTick()
  assert.equal(e.updates(),before);assert.equal(e.state.jsonErrors.value_schema,'')
  assert.deepEqual(JSON.parse(e.state.jsonDrafts.value_schema),{minimum:2,maximum:9})
  e.state.updateJson(field,input('{"minimum":2,"maximum":8}'));await e.nextTick()
  assert.deepEqual(e.props.modelValue.value_schema,{minimum:2,maximum:8})
})

test('JSON drafts are isolated by path and removed for hidden, locked or switched records',async context=>{
  const e=await editor(context,'config-definition',{id:'one',value_type:'object',is_secret:false,value_schema:{},initial_value:{a:1}})
  const schema=e.field('value_schema'),initial=e.field('initial_value')
  e.state.updateJson(schema,input('{'));e.state.updateJson(initial,input('['));await e.nextTick()
  e.state.discardJsonDraft(schema);await e.nextTick()
  assert.equal(e.state.jsonErrors.value_schema,'');assert.match(e.state.jsonErrors.initial_value,/JSON/)
  e.props.modelValue={...e.props.modelValue,value_type:'string'};await e.nextTick()
  assert.equal(e.state.jsonDrafts.initial_value,undefined);assert.equal(e.state.jsonErrors.initial_value,undefined)
  const before=e.updates();e.state.updateJson(initial,input('{"a":9}'));assert.equal(e.updates(),before)
  e.state.updateJson(schema,input('{'));e.props.dependencies.lockedPaths=['value_schema'];await e.nextTick()
  assert.equal(e.state.jsonErrors.value_schema,undefined)
  e.state.updateJson(schema,input('{}'));assert.equal(e.updates(),before)
  e.props.dependencies.lockedPaths=[];await e.nextTick()
  e.state.updateJson(e.field('value_schema'),input('{'));await e.nextTick()
  e.props.modelValue={id:'two',value_type:'object',value_schema:{type:'object'},initial_value:{b:2}};await e.nextTick()
  assert.equal(e.state.jsonErrors.value_schema,'');assert.deepEqual(JSON.parse(e.state.jsonDrafts.value_schema),{type:'object'})
  const source=await readFile(new URL('../src/components/RegistryResourceEditor.vue',import.meta.url),'utf8')
  assert.match(source,/:aria-invalid="!!jsonErrors\[field.path\]"/)
  assert.match(source,/@click="discardJsonDraft\(field\)"/)
})

test('advanced inventory text uses a text field rather than a JSON writer',async context=>{
  const e=await editor(context,'artifact',{inventory_folder:'diffusion_models',inventory_node_type:'CustomLoader',inventory_input_name:'model_name'})
  const html=await e.html()
  assert.match(html,/<input[^>]*aria-label="ComfyUI 库存目录 \/ Inventory folder"[^>]*value="diffusion_models"/)
  assert.equal(e.state.jsonDrafts.inventory_folder,undefined)
  const before=e.updates();e.state.updateJson(e.field('inventory_folder'),input('"other"'));assert.equal(e.updates(),before)
  e.state.updateAdvancedText(e.field('inventory_folder'),input('custom_models'));await e.nextTick()
  assert.equal(e.props.modelValue.inventory_folder,'custom_models')
  e.props.dependencies.lockedPaths=['inventory_folder'];await e.nextTick()
  assert.match(await e.html(),/registry-readonly[^>]*>custom_models<\/output>/)
  const locked=e.updates();e.state.updateAdvancedText({path:'inventory_folder'},input('no'));assert.equal(e.updates(),locked)
})
