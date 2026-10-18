import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, reactive, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

const field = (type: string, title: string, extra = {}) => ({type,title,'x-field-definition':{
  field_definition_version_id:`v-${title}`,label:title,value_type:type==='string'?'text':type==='array'?'list':type,unit:'',description:`${title}的含义`},...extra})
const structure = field('object','自定义数据',{properties:{
  'mood.note':field('string','自定义情绪',{default:'不可自动填入的默认',minLength:1}),
  active:field('boolean','启用'), count:field('integer','数量',{minimum:0}),
  rows:field('array','记录',{items:field('object','元素',{properties:{text:field('string','正文')},additionalProperties:true})}),
  mode:{anyOf:[field('string','可选档位',{enum:['甲','乙']}),{type:'null'}]},
},required:['mood.note'],additionalProperties:true})
async function modules(context: any) {
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  return {vite, service:await vite.ssrLoadModule('/src/services/structuredInputForm.ts'),
    Editor:(await vite.ssrLoadModule('/src/components/StructuredJsonEditor.vue')).default}
}
async function setup(Component: any, props: any) {
  let state:any;const events:any[]=[]
  // Client component lifecycle: SSR intentionally stops immediate watchers.
  const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(event:string,value:any){events.push([event,value]);if(event==='update:modelValue')props.modelValue=value}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({})
  return {state,events}
}
const render = (Component:any,props:any) => renderToString(createSSRApp({render:()=>h(Component,props)}))

test('exact field schema drives arbitrary nested fields and never copies defaults or examples',async context=>{
  const {Editor}=await modules(context)
  const props=reactive({modelValue:{} as any,label:'样例',schemas:[{...structure,examples:[{'mood.note':'禁止使用的示例'}]}]})
  const {state,events}=await setup(Editor,props)
  assert.equal(events.filter(row=>row[0]==='update:modelValue').length,0)
  state.keyDraft.value='mood.note';state.add();assert.deepEqual(props.modelValue,{'mood.note':''})
  state.child('mood.note','谨慎');state.keyDraft.value='active';state.add();state.child('active',false)
  state.keyDraft.value='count';state.add();state.child('count',0)
  state.keyDraft.value='unregistered';state.add();assert.ok(!Object.hasOwn(props.modelValue,'unregistered'))
  state.remove('count');assert.ok(!Object.hasOwn(props.modelValue,'count'))
  assert.deepEqual(props.modelValue,{'mood.note':'谨慎',active:false})
  const html=await render(Editor,props)
  assert.match(html,/自定义情绪/);assert.match(html,/请选择字段/);assert.doesNotMatch(html,/按输入要求填写字段标识|禁止使用的示例/)
})
test('nullable integer enum object and exact path errors use non-null rules without losing null',async context=>{
  const {Editor,service:s}=await modules(context)
  const number={anyOf:[field('integer','可空数量',{minimum:2,maximum:8}),{type:'null'}]}
  assert.deepEqual(s.jsonAllowedTypes([number]).map((row:any)=>row.value),['number','null'])
  assert.deepEqual(s.jsonInputErrors(null,[number]),[])
  assert.ok(s.jsonInputErrors(2.5,[number]).length)
  assert.match(await render(Editor,{modelValue:3,label:'可空数量',schemas:[number]}),/step="1"/)
  assert.match(await render(Editor,{modelValue:'甲',label:'档位',schemas:[structure.properties.mode]}),/选择内容/)
  assert.match(await render(Editor,{modelValue:'甲',label:'档位',schemas:[structure.properties.mode]}),/aria-label="档位类型"/)
  assert.doesNotMatch(await render(Editor,{modelValue:'甲',label:'档位',schemas:[structure.properties.mode]}),/<textarea/)
  const nullable={anyOf:[structure,{type:'null'}]}
  assert.equal(s.jsonValuePath('value','mood.note'),'value["mood.note"]')
  const html=await render(Editor,{modelValue:{},label:'样例',schemas:[nullable],fieldErrors:{'value["mood.note"]':['必须提供此字段']}})
  assert.match(html,/自定义情绪：未提供（必填）/);assert.match(html,/必须提供此字段/)
})
test('read-only values distinguish missing null zero false empty containers and retain malformed original',async context=>{
  const {Editor}=await modules(context)
  for(const [value,expected] of [[undefined,'未提供（缺失）'],[null,'明确空值（null）'],['','空字符串'],[0,'0'],[false,'否（false）'],[[],'空列表'],[{},'空对象']] as const){
    const html=await render(Editor,{modelValue:value,label:'实际值',schemas:[],readonly:true})
    assert.ok(html.includes(expected));assert.doesNotMatch(html,/<textarea|<input|<select|添加字段|删除/)
  }
  const html=await render(Editor,{modelValue:{raw:'错误但真实的正文'},label:'数值',schemas:[field('integer','数值')],readonly:true})
  assert.match(html,/错误但真实的正文/);assert.match(html,/需要整数/)
  const {state,events}=await setup(Editor,reactive({modelValue:false,label:'锁定',readonly:true}))
  state.commit(true);state.fill();state.remove('x');assert.ok(!events.some(row=>row[0]==='update:modelValue'))
})
test('lists add remove nested values, preserve unknown input and render long content without raw-JSON-only UI',async context=>{
  const {Editor}=await modules(context)
  const props=reactive({modelValue:[] as any,label:'记录',schemas:[structure.properties.rows]})
  const {state}=await setup(Editor,props);state.add();state.add();state.child('0',{text:'正文'});state.remove('1')
  assert.deepEqual(props.modelValue,[{text:'正文'}])
  const html=await render(Editor,{modelValue:{'mood.note':'x'.repeat(2000),extra:'未登记但保留'},label:'样例',schemas:[structure],readonly:true})
  assert.match(html,/未映射字段/);assert.match(html,/未登记但保留/);assert.match(html,/x{2000}/)
})
test('preview retains raw input and default source, ignores stale results and creates no requests on mount',async context=>{
  const {vite}=await modules(context)
  const {dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
  const Preview=(await vite.ssrLoadModule('/src/components/DataStructureValuePreview.vue')).default
  const calls:any[]=[]
  api.schema=async(id:string)=>({valid:true,json_schema:structure,errors:{}})
  api.preview=async(id:string,value:any)=>{calls.push([id,value]);return{preview_only:true,structure_version_id:id,valid:true,value:{...value,'mood.note':'配置默认'},defaults:[{path:'value["mood.note"]',value:'配置默认',source:'configured_default',structure_version_id:id,field_definition_version_id:'fv'}],errors:{}}}
  const props=reactive({structureVersionId:'v1'});const {state,events}=await setup(Preview,props)
  await state.load();assert.deepEqual(calls,[]);assert.equal(state.value.value,undefined)
  state.edit({active:false,count:0});await state.preview()
  assert.deepEqual(state.value.value,{active:false,count:0});assert.equal(state.result.value.defaults[0].value,'配置默认');assert.deepEqual(events,[])
  const html=await render({...Preview,setup:()=>state},props)
  assert.doesNotMatch(html,/需要添加 自定义情绪/,'explicit source defaults must not produce a contradictory missing-field alert')
  assert.match(html,/采用显式缺键默认值/)
  state.draftErrors.value=['尚未完成的数字'];await state.preview();assert.equal(calls.length,1,'invalid editor draft cannot submit previous numeric value');state.draftErrors.value=[]
  let release:any;api.preview=()=>new Promise(resolve=>release=resolve)
  const pending=state.preview();state.edit({count:3});release({preview_only:true,structure_version_id:'v1',valid:true,value:{count:0}});await pending
  assert.equal(state.result.value,null)
  api.preview=async()=>{throw new Error('读取失败')};await state.preview();assert.match(state.error.value,/读取失败/);assert.deepEqual(state.value.value,{count:3})
  props.structureVersionId='v2';await nextTick();assert.equal(state.value.value,undefined)
})

test('reused field labels include property names only when ambiguous in the same object',async context=>{
  const {Editor}=await modules(context)
  const schema=field('object','样例',{properties:{
    note:field('string','说明'),optional:field('string','说明'),count:field('integer','数量'),
  }})
  const html=await render(Editor,{modelValue:{note:'已填'},label:'样例',schemas:[schema]})
  assert.match(html,/aria-label="说明 · note"/)
  assert.match(html,/说明 · optional/)
  assert.doesNotMatch(html,/数量 · count/)
})
test('media reads only explicit exact reference and rejects stale failed wrong-kind details without replacing raw id',async context=>{
  const {vite}=await modules(context)
  const {repository:repo}=await vite.ssrLoadModule('/src/services/repository.ts')
  const Media=(await vite.ssrLoadModule('/src/components/StructureMediaValue.vue')).default
  const schemas=[{'x-media-types':['image'],'x-media-usage':'参考外观'}]
  const props=reactive({modelValue:{asset_id:'id1'},label:'参考',schemas,selectable:true})
  const {state,events}=await setup(Media,props)
  let calls=0;repo.getAsset=async(id:string)=>{calls++;return{id,name:'真实名称',mimeType:'image/png',isActive:true,mediaUrl:'/fixture.png'}}
  assert.equal(calls,0);await state.load();assert.equal(state.asset.value.name,'真实名称')
  let release:any;repo.getAsset=()=>new Promise(resolve=>release=resolve);const pending=state.load()
  props.modelValue={asset_id:'id2'};await nextTick();release({id:'id1',mimeType:'image/png'});await pending;assert.equal(state.asset.value,null)
  repo.getAsset=async()=>({id:'id2',mimeType:'video/mp4'});await state.load();assert.match(state.error.value,/类型不符合/);assert.deepEqual(props.modelValue,{asset_id:'id2'})
  state.select({id:'bad',mimeType:'video/mp4'});assert.deepEqual(events,[])
  state.select({id:'id3',mimeType:'image/png'});assert.deepEqual(props.modelValue,{asset_id:'id3'})
  const html=await render(Media,{...props,readonly:true});assert.doesNotMatch(html,/选择已有素材|上传本地文件/);assert.match(html,/参考外观/)
})
test('sample asset picker cannot upload even if upload handler is called',async context=>{
  const {vite,Editor}=await modules(context)
  const media=field('object','素材',{'x-media-types':['image'],properties:{asset_id:{type:'string',format:'uuid'}}})
  const schema=field('object','样例',{properties:{reference:media}})
  const html=await render(Editor,{modelValue:{reference:{}},label:'样例',schemas:[schema],selectMedia:true})
  assert.match(html,/选择已有素材/)
  assert.doesNotMatch(html,/上传本地文件/,'nested preview media must pass the read-only-library mode to its picker')
  const {repository:repo}=await vite.ssrLoadModule('/src/services/repository.ts')
  const Picker=(await vite.ssrLoadModule('/src/components/ExecutionInputAssetPicker.vue')).default
  repo.createAsset=async()=>{throw new Error('preview must not upload')}
  const {state,events}=await setup(Picker,reactive({kind:'image',allowUpload:false}))
  state.file.value={name:'test.png'};state.name.value='test';await state.upload();assert.deepEqual(events,[])
})
test('malformed read-only media retains scalar and extra original fields instead of claiming absence',async context=>{
  const {Editor}=await modules(context)
  const schema=field('object','素材',{'x-media-types':['image'],properties:{asset_id:{type:'string',format:'uuid'}},required:['asset_id'],additionalProperties:false})
  for(const value of ['错误原文',{asset_id:123,extra:'需保留'}]){
    const html=await render(Editor,{modelValue:value,label:'素材',schemas:[schema],readonly:true})
    assert.match(html,/素材引用格式不正确/);assert.doesNotMatch(html,/尚未提供素材引用/)
    assert.ok(html.includes(typeof value==='string'?value:'需保留'))
    if(typeof value!=='string')assert.match(html,/123/)
  }
})
