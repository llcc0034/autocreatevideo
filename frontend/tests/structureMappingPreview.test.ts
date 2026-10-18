import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

const source = {type:'object',properties:{'custom.rows':{type:'array',items:{type:'object',properties:{code:{type:'string'},zero:{type:'integer'},active:{type:'boolean'},nested:{type:'array',items:{type:'string'}}}}}}}
const target = {type:'object',properties:{result:{type:'array',items:{type:'integer'}},enabled:{type:'boolean',default:false}},required:['result'],additionalProperties:true}
const options = {schema_version:1,nodes:['path','lookup','object','array','literal','each'].map(value=>({value,label:value})),scopes:[{value:'root',label:'整个来源'},{value:'item',label:'当前条目'}]}
async function modules(context:any) {
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close()); return vite
}
function setup(context:any,Component:any,props:any={}) {
  let state:any;const events:any[]=[]
  const renderer=createRenderer({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit:(event:string,value:any)=>{events.push([event,value]);if(event==='update:modelValue')props.modelValue=value}});return()=>h('div')}})
  app.provide(ssrContextKey,{modules:new Set()});app.mount({});context.after(()=>app.unmount())
  return {state,events}
}

test('center preview uses exact versions, keeps original JSON and ignores stale reports without configuration writes',async context=>{
  const vite=await modules(context),{dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
  const Component=(await vite.ssrLoadModule('/src/components/DataStructureMappingPreview.vue')).default
  const calls:any[]=[]
  api.mappingOptions=async()=>options;api.schema=async(id:string)=>({valid:true,json_schema:id==='s1'?source:target,errors:{}})
  api.mappingPreview=async(...args:any[])=>{calls.push(args);return{preview_only:true,valid:true,parsed:true,value:{result:[0],enabled:false},defaults:[{value:false}],errors:{}}}
  for(const key of ['create','update','transition'])api[key]=()=>assert.fail('preview must not save configuration')
  const {state}=setup(context,Component);await state.loadOptions();assert.deepEqual(calls,[])
  state.selectStructure('source','s1');state.selectStructure('target','t2');await state.loadSchemas()
  const mapping={kind:'object',fields:{result:{kind:'each',source:{kind:'path',scope:'root',path:['custom.rows']},item:{kind:'path',scope:'item',path:['zero']}}}}
  state.editMapping(mapping);state.sourceText.value='  {"custom.rows":[{"zero":0,"active":false}]}  '
  await state.preview();assert.deepEqual(calls[0],['t2','s1',state.sourceText.value,{schema_version:1,node:mapping}]);assert.equal(state.result.value.value.enabled,false)
  state.draftErrors.value=['数字尚未完成'];assert.equal(state.result.value,null);await state.preview();assert.equal(calls.length,1)
  state.draftErrors.value=[]
  let release:any;api.mappingPreview=()=>new Promise(resolve=>release=resolve)
  const pending=state.preview();state.sourceText.value='{bad JSON';release({valid:true,value:'stale'});await pending;assert.equal(state.result.value,null)
  api.mappingPreview=async()=>{throw Error('隔离接口失败')};await state.preview();assert.match(state.error.value,/隔离接口失败/);assert.equal(state.sourceText.value,'{bad JSON')
  state.selectStructure('source','layout-b');await state.loadSchemas();assert.deepEqual(state.mapping.value,mapping);assert.match(state.notice.value,/未自动改写/)
  api.schema=async()=>({valid:false,json_schema:null,errors:{nodes:['字段版本缺失']}});await state.loadSchemas()
  assert.equal(state.ready.value,false);assert.deepEqual(state.schemaErrors.value,{'来源.nodes':['字段版本缺失'],'目标.nodes':['字段版本缺失']})
  let releaseSchema:any
  api.schema=(id:string)=>id==='layout-b'?new Promise(resolve=>releaseSchema=resolve):Promise.resolve({valid:true,json_schema:target,errors:{}})
  const oldSchemas=state.loadSchemas()
  api.schema=async()=>({valid:true,json_schema:source,errors:{}})
  state.selectStructure('source','fresh-layout');await state.loadSchemas()
  releaseSchema({valid:false,json_schema:null,errors:{nodes:['迟到旧错误']}});await oldSchemas
  assert.equal(state.schemaError.value,'');assert.deepEqual(state.sourceSchema.value,source)
})

test('schema lookup navigation and nested each use explicit root/item paths without evaluating business values',async context=>{
  const vite=await modules(context),service=await vite.ssrLoadModule('/src/services/structureMappingPreview.ts')
  const rows={kind:'path',scope:'root',path:['custom.rows']}
  const node={kind:'lookup',collection:rows,key_path:['code'],key:{kind:'literal',value:'x'},value_path:['nested']}
  assert.deepEqual(service.previewMappingSchema(node,source),{type:'array',items:{type:'string'}})
  assert.deepEqual(service.previewMappingSchema({...node,value_path:['absent']},source),{})
  assert.deepEqual(service.previewMappingSchema({kind:'path',scope:'item',path:['zero']},source,source.properties['custom.rows'].items),{type:'integer'})
  assert.deepEqual(service.previewMappingSchema({kind:'path',scope:'item',path:[]},source),{})
  assert.deepEqual(service.initializePreviewMapping('path',target,[]),{kind:'path',scope:'root'})
  const Editor=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const props=reactive({modelValue:{kind:'each',source:rows,item:{kind:'path',scope:'item',path:['zero']}},schema:{type:'array',items:{type:'integer'}},label:'结果',nodes:options.nodes,previewContext:{schema:source,scopes:options.scopes},fields:[],outputs:[],scopes:[],priorStages:[]})
  const {state}=setup(context,Editor,props)
  assert.deepEqual(state.eachSchema.value,source.properties['custom.rows'].items)
  assert.equal(state.allowAdditional.value,false)
  assert.ok(!state.nodeOptions.value.some((row:any)=>row.value==='object'))
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/当前条目/);assert.doesNotMatch(html,/受控内容来源|上游已采用输出|新增字段名/)
})

test('lookup editor supports key and value selectors; unfinished nested literals invalidate parent preview',async context=>{
  const vite=await modules(context),Editor=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const props=reactive({modelValue:{kind:'lookup',collection:{kind:'path',scope:'root',path:['custom.rows']},key_path:['code'],key:{kind:'literal',value:'x'},value_path:['active']} as any,schema:{type:'boolean'},label:'是否启用',nodes:options.nodes,previewContext:{schema:source,scopes:options.scopes},fields:[],outputs:[],scopes:[],priorStages:[]})
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/稳定 key 路径/);assert.match(html,/找到后读取的内容路径/);assert.match(html,/不会选择第一项/)
  const {state,events}=setup(context,Editor,props)
  state.childDrafts.value={key:['需要有效数字']};await nextTick();assert.deepEqual(events.at(-1),['draftValidity',['需要有效数字']])
  props.modelValue={kind:'array',items:[{kind:'literal',value:0},{kind:'literal',value:false}]};await nextTick()
  state.childDrafts.value={'item:0':['未完成']};state.removeItem(0);assert.equal(props.modelValue.items.length,2)
  state.move(0,1);assert.equal(props.modelValue.items[0].value,0)
  state.childDrafts.value={};state.move(0,1);assert.equal(props.modelValue.items[0].value,false)
})

test('preview-only structure selector lists draft versions but regular consumers remain published-only',async context=>{
  const vite=await modules(context),{dataStructures:api}=await vite.ssrLoadModule('/src/services/dataStructures.ts')
  const Selector=(await vite.ssrLoadModule('/src/components/DataStructureVersionSelect.vue')).default
  const calls:any[]=[];api.list=async(...args:any[])=>{calls.push(args);return{count:0,results:[]}}
  for(const previewOnly of [false,true]){const {state}=setup(context,Selector,reactive({modelValue:'',previewOnly}));await state.load()}
  assert.deepEqual(calls,[[1,'','published'],[1,'','']])
})

test('semantic object authoring starts with required fields only and keeps optional fields explicitly selectable',async context=>{
  const vite=await modules(context),service=await vite.ssrLoadModule('/src/services/structureMappingPreview.ts')
  const Editor=(await vite.ssrLoadModule('/src/components/RecipeStructureMappingEditor.vue')).default
  const schema={type:'object',required:['shot'],additionalProperties:false,properties:{
    shot:{type:'integer',title:'镜序号'},action:{type:'string',title:'动作'},setting:{type:'string',title:'场景描述'},enabled:{type:'boolean',default:false}}}
  const original=structuredClone(schema)
  const initial=service.initializePreviewMapping('object',schema,[])
  assert.deepEqual(initial,{kind:'object',fields:{shot:{}}})
  assert.deepEqual(service.initializePreviewMapping('object',{type:'object',properties:{note:{type:'string'}}},[]),{kind:'object',fields:{}})
  assert.deepEqual(service.initializePreviewMapping('object',{...schema,required:['shot','enabled']},[schema]),{kind:'object',fields:{shot:{}}})
  assert.deepEqual(schema,original)
  const props=reactive({modelValue:initial,schema,label:'镜头',nodes:options.nodes,previewContext:{schema:source,scopes:options.scopes},fields:[],outputs:[],scopes:[],priorStages:[],disabled:false})
  const {state}=setup(context,Editor,props)
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/配置 动作 的来源/);assert.match(html,/配置 场景描述 的来源/)
  assert.doesNotMatch(html,/移除字段/)
  state.addDeclaredField('action');await nextTick();assert.deepEqual(props.modelValue.fields,{shot:{},action:{}})
  state.child('action',{kind:'path',scope:'item',path:['motion']});await nextTick()
  const saved=structuredClone(JSON.parse(JSON.stringify(props.modelValue)))
  props.disabled=true;state.addDeclaredField('setting');await nextTick();assert.deepEqual(props.modelValue,saved)
  assert.equal(state.omittedFields.value.includes('setting'),true)
  assert.deepEqual(props.modelValue.fields.action,{kind:'path',scope:'item',path:['motion']})
})
