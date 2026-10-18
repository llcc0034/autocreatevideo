import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'

const raw = {schema_version:1,temperature_minimum:0,response_format_types:[{value:'text',label:'文本'},{value:'json_object',label:'JSON 对象'},{value:'json_schema',label:'JSON Schema'}],assembly_description:'冻结 → 请求正文同名字段',boundary_description:'并非模型支持证明'}
async function setup(context:any) {
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close());return vite
}
test('catalog, absence, zero, null, finite bounds and provider extensions remain distinct',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelChatRequestOptions.ts'),options=m.adaptChatRequestOptions(raw)
  for(const value of [{},{temperature:0},{temperature:null,response_format:null},{temperature:9,response_format:{type:'provider_specific',extra:false}}])assert.deepEqual(m.commonChatOptionErrors(value,options),[])
  for(const value of [undefined,[],{temperature:true},{temperature:-1},{temperature:Infinity},{response_format:{}},{response_format:{type:' '}}])assert.ok(m.commonChatOptionErrors(value,options).length)
  assert.ok(m.commonChatOptionErrors({},null).length)
  for(const value of [null,{}, {...raw,temperature_minimum:'0'},{...raw,response_format_types:[{value:'text',label:'文本'},{value:'text',label:'重复'}]}])assert.throws(()=>m.adaptChatRequestOptions(value))
  const original={temperature:0,response_format:{type:'json_schema',json_schema:{name:'example'}},thinking:{type:'disabled'}}
  assert.deepEqual(m.chatExtensions(original,['temperature','response_format']),{thinking:{type:'disabled'}})
  assert.deepEqual(m.replaceChatExtensions(original,{other:[false,null,0]},['temperature','response_format']),{temperature:0,response_format:original.response_format,other:[false,null,0]})
  assert.throws(()=>m.replaceChatExtensions(original,{temperature:2},['temperature','response_format']),/重复配置/)
  assert.throws(()=>m.replaceChatExtensions(original.response_format,{type:'text'},['type']),/重复配置/)
  assert.equal(original.thinking.type,'disabled')
})
test('full specification cannot duplicate typed request editing, including dotted operation keys',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelChatRequestOptions.ts')
  const original={visual_rules:{chat_request_options:{temperature:0}},prompt_normalization:{chat_request_options:{}},text_execution:{operations:{'prompt.draft':{chat_request_options:{thinking:{type:'disabled'}}}}}}
  m.assertChatOptionsUnchanged(original,{...original,other:'changed'})
  m.assertChatOptionsUnchanged({visual_rules:{chat_request_options:{a:1,b:2}}},{visual_rules:{chat_request_options:{b:2,a:1}}})
  for(const path of ['visual_rules','prompt_normalization','text_execution']){const next=structuredClone(original);delete (next as any)[path];assert.throws(()=>m.assertChatOptionsUnchanged(original,next),/固定表单/)}
  const next=structuredClone(original);next.text_execution.operations['prompt.draft'].chat_request_options.thinking.type='enabled'
  assert.throws(()=>m.assertChatOptionsUnchanged(original,next),/固定表单/)
})
test('editor controls preserve extensions and invalid drafts, and do not emit when readonly or catalog missing',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/modelChatRequestOptions.ts'),{default:Editor}=await vite.ssrLoadModule('/src/components/ModelChatRequestOptionsEditor.vue')
  const {createSSRApp,h,reactive,nextTick}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props=reactive({modelValue:{thinking:{type:'disabled'}} as any,options:m.adaptChatRequestOptions(raw),disabled:false,label:'测试请求选项'})
  let state:any,updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:unknown)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}}});return()=>h('div')}}))
  assert.equal(updates,0)
  state.temperatureModeChanged('number');assert.equal(props.modelValue.temperature,0)
  state.temperatureChanged('0.75');assert.equal(props.modelValue.temperature,0.75)
  for(const invalid of ['', '-1','Infinity']){state.temperatureChanged(invalid);assert.ok(state.numericError.value);assert.equal(props.modelValue.temperature,0.75)}
  state.temperatureModeChanged('null');assert.equal(props.modelValue.temperature,null)
  state.temperatureModeChanged('omit');assert.equal(Object.hasOwn(props.modelValue,'temperature'),false)
  state.formatChanged('json_schema');state.extensionsChanged({json_schema:{name:'example',strict:true}},true)
  state.formatChanged('json_object');assert.equal(props.modelValue.response_format.json_schema.strict,true)
  state.extensionsChanged({type:'text'},true);assert.match(state.ownError.value,/重复配置/)
  state.extensionsChanged({temperature:1});assert.match(state.ownError.value,/重复配置/)
  state.temperatureChanged('0');assert.equal(props.modelValue.thinking.type,'disabled')
  state.formatChanged('null');assert.equal(props.modelValue.response_format,null)
  state.formatChanged('omit');assert.equal(Object.hasOwn(props.modelValue,'response_format'),false)
  state.extensionsChanged({thinking:{type:'disabled'},extra:[false,null]})
  await nextTick();assert.deepEqual(state.errors.value,[])
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/采样温度/);assert.match(html,/不发送此字段/);assert.match(html,/冻结 → 请求正文同名字段/)
  props.disabled=true;const before=updates
  state.temperatureModeChanged('null');state.temperatureChanged('1');state.formatChanged('text');state.extensionsChanged({});state.commit({})
  assert.equal(updates,before)
  const readonly=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  for(const tag of readonly.match(/<(?:input|select|textarea|button)\b[^>]*>/g)||[])assert.match(tag,/disabled/)
  props.disabled=false;props.options=null;state.temperatureChanged('2');state.formatChanged('text');assert.equal(updates,before)
  assert.ok(state.errors.value.some((s:string)=>s.includes('目录')))
})
test('native model fields use one form and no advanced duplicate options field',async context=>{
  const vite=await setup(context),m=await vite.ssrLoadModule('/src/services/registryResourceSchema.ts')
  const fields=m.resourceEditorFields('model',{specification:{capabilities:['prompt.normalize','visual_rules.generate']},modality:'text'},{providers:[],modelProfiles:[],deployments:[],runtimeHosts:[],workflowVersions:[]})
  const rows=fields.filter((f:any)=>f.path.endsWith('.chat_request_options'))
  assert.equal(rows.length,2);assert.ok(rows.every((f:any)=>f.kind==='chat-options'&&!f.advanced))
  assert.ok(m.chatRequestOptionErrors({extra:{api_key:'forbidden'}}).length)
  assert.ok(m.chatRequestOptionErrors({extra:{max_tokens:1}}).length)
})

test('mounted validity feedback stabilizes equivalent schemas and still reports changed nested errors',async context=>{
  const vite=await setup(context),{default:Editor}=await vite.ssrLoadModule('/src/components/StructuredJsonEditor.vue')
  const {createRenderer,h,reactive,nextTick,ssrContextKey}=await import('vue')
  const props=reactive({modelValue:{thinking:{type:'disabled'}},label:'扩展',fixedObject:true,schemas:[{type:'object'}]})
  const events:string[][]=[],failures:any[]=[];let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:string[]){if(event==='validity'){events.push(value);props.schemas=props.schemas.map(row=>({...row}))}}});return()=>h('div')}})
  app.config.errorHandler=error=>failures.push(error);app.provide(ssrContextKey,{modules:new Set()});app.mount({});context.after(()=>app.unmount())
  await nextTick();assert.deepEqual(failures,[]);assert.deepEqual(events,[[]])
  for(let i=0;i<3;i++){props.schemas=[{type:'object'}];state.childrenErrors.value={thinking:[]};await nextTick()}
  assert.equal(events.length,1)
  state.childrenErrors.value={thinking:['嵌套值缺失']};await nextTick();assert.deepEqual(events.at(-1),['嵌套值缺失']);assert.equal(events.length,2)
  state.childrenErrors.value={thinking:[]};await nextTick();assert.deepEqual(events.at(-1),[]);assert.equal(events.length,3);assert.deepEqual(failures,[])
})
