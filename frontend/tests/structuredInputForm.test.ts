import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'

async function modules(context: any) {
  const vite=await createServer({logLevel:'silent',server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  return { service:await vite.ssrLoadModule('/src/services/structuredInputForm.ts'),
    Editor:(await vite.ssrLoadModule('/src/components/StructuredJsonEditor.vue')).default,
    form:await vite.ssrLoadModule('/src/services/executionRequestForm.ts') }
}
test('speech-language schema guidance and pattern are shown without guessed defaults', async context => {
  const {service:s,Editor} = await modules(context)
  const schema = {type:'string',title:'本句对白语言',description:'逐句声明；不继承项目语言，台词原文不翻译。',pattern:'^[^\\x00-\\x1f\\x7f<>\\[\\]{}]*$(?![\\s\\S])'}
  for (const value of ['Chinese', 'English', 'zh-Hans', '']) assert.deepEqual(s.jsonInputErrors(value,[schema]),[])
  for (const value of ['English\n','[Chinese]','{language}','<d>']) assert.ok(s.jsonInputErrors(value,[schema]).length)
  assert.ok(s.jsonInputErrors('English',[{type:'string',pattern:'['}]).length)
  const html = await renderToString(createSSRApp(Editor,{modelValue:'English',label:'本句对白语言',schemas:[schema]}))
  assert.match(html,/逐句声明；不继承项目语言，台词原文不翻译/)
})
test('only declared server defaults defer missing keys without writing form values',async context=>{
  const {service:s,Editor}=await modules(context)
  const logical={type:'object',properties:{groups:{type:'array',items:{type:'object',properties:{count:{type:'integer',default:4}},required:['count']}}},required:['groups']}
  const native={type:'object',properties:{groups:{type:'array',items:{type:'object',properties:{count:{type:'integer',minimum:0}},required:['count']}}},required:['groups']}
  const raw={groups:[{}]}, original=structuredClone(raw)
  assert.ok(s.jsonInputErrors(raw,[logical,native]).length,'target annotation alone does not enable defaults')
  assert.deepEqual(s.jsonInputErrors(raw,[logical,native],'输入',[logical]),[])
  for(const count of [null,false,'',-1]) assert.ok(s.jsonInputErrors({groups:[{count}]},[logical,native],'输入',[logical]).length)
  assert.deepEqual(s.jsonInputErrors({groups:[{count:0}]},[logical,native],'输入',[logical]),[])
  let errors:any
  await renderToString(createSSRApp(Editor,{modelValue:raw,label:'输入',schemas:[logical,native],defaultSchemas:[logical],onValidity:(value:any)=>errors=value}))
  assert.deepEqual(errors,[])
  assert.deepEqual(raw,original,'no frontend value/default writer')
  assert.ok(s.jsonInputErrors({},[logical,native],'输入',[logical]).length,'parent without default remains required')
})

test('schema-derived numeric forms keep blank values, all target bounds and exact JSON types',async context=>{
  const {service:s,Editor}=await modules(context)
  const schemas=[{type:'number',maximum:8},{type:'integer',minimum:1}]
  assert.deepEqual(s.jsonAllowedTypes(schemas).map(row=>row.value),['number'])
  assert.equal(s.emptyJsonInput(schemas),null)
  for (const value of [null,{},false,'2',0,1.2,9]) assert.ok(s.jsonInputErrors(value,schemas).length)
  assert.deepEqual(s.jsonInputErrors(2,schemas),[])
  const props=reactive({modelValue:null as any,label:'本轮计划集数',schemas,disabled:false})
  let state:any
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='update:modelValue')props.modelValue=value}});return()=>h('div')}}))
  state.number('2');assert.equal(props.modelValue,2)
  state.number('');assert.equal(props.modelValue,2);assert.ok(state.errors.value.length)
  state.number('1.5');assert.ok(state.errors.value.length)
  state.number('3');assert.deepEqual(state.errors.value,[])
  state.changeType('object');assert.equal(props.modelValue,3)
  props.disabled=true;state.number('4');assert.equal(props.modelValue,3)
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/type="number"/);assert.match(html,/step="1"/);assert.match(html,/min="1"/);assert.match(html,/max="8"/)
  assert.match(html,/disabled/);assert.doesNotMatch(html,/<select|添加字段|字段集合/)
})
test('objects expose declared fields, arrays keep item types, generic values explain formats and null',async context=>{
  const {service:s,Editor}=await modules(context)
  const schemas=[{type:'object',properties:{count:{type:'integer',title:'数量',minimum:1},enabled:{type:'boolean'}},required:['count'],additionalProperties:false}]
  assert.deepEqual(s.jsonPropertyOptions(schemas),['count','enabled'])
  assert.equal(s.jsonPropertyAllowed(schemas,'arbitrary'),false)
  assert.deepEqual(s.jsonChildSchemas(schemas,'count',false),[{type:'integer',title:'数量',minimum:1}])
  assert.ok(s.jsonInputErrors({},schemas)[0].includes('数量'))
  assert.deepEqual(s.jsonInputErrors({count:2,enabled:false},schemas),[])
  assert.ok(s.jsonInputErrors([1,'2'],[{type:'array',items:{type:'integer'}}]).length)
  const props=reactive({modelValue:{} as any,label:'生成计划',schemas,disabled:false})
  let state:any
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='update:modelValue')props.modelValue=value}});return()=>h('div')}}))
  state.keyDraft.value='arbitrary';state.add();assert.deepEqual(props.modelValue,{})
  state.keyDraft.value='count';state.add();assert.deepEqual(props.modelValue,{count:null})
  state.child('count',2);assert.deepEqual(state.errors.value,[])
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/数量/);assert.match(html,/请选择字段/);assert.doesNotMatch(html,/填写接口文档/)
  const generic=await renderToString(createSSRApp({render:()=>h(Editor,{modelValue:{},label:'扩展内容'})}))
  assert.match(generic,/字段集合（对象）/);assert.match(generic,/不是制作步骤分组/);assert.match(generic,/不等于不提供/)
  assert.doesNotMatch(generic,/不向模型发送额外选项/)
  assert.deepEqual(s.jsonAllowedTypes([{type:'integer'},{type:'object'}]),[])
  assert.deepEqual(s.jsonChoices([{type:'integer',enum:[1,2,3]},{minimum:2,const:2}]),[2])
})

test('unfinished numeric input remains visible through parent re-renders without committing the old value',async context=>{
  const {Editor}=await modules(context)
  const props=reactive({modelValue:0 as unknown,label:'时长',schemas:[{type:'number'}],disabled:false})
  const writes:unknown[]=[]
  let state:any
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:unknown){if(event==='update:modelValue'){writes.push(value);props.modelValue=value}}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount())
  for(const raw of ['', '1e']){
    state.number(raw);await nextTick()
    assert.equal(state.numberInput.value,raw);assert.equal(props.modelValue,0);assert.equal(writes.length,0)
    assert.ok(state.draftErrors.value.length)
    props.disabled=true;props.schemas=[{type:'number'}];await nextTick()
    assert.equal(state.numberInput.value,raw);assert.ok(state.draftErrors.value.length)
    state.number('9');assert.equal(writes.length,0)
    props.disabled=false;await nextTick()
    assert.equal(state.numberInput.value,raw)
  }
  state.number('0');await nextTick()
  assert.equal(state.numberInput.value,0);assert.deepEqual(state.draftErrors.value,[]);assert.deepEqual(writes,[0])
  state.number('');props.modelValue=42;await nextTick()
  assert.equal(state.numberInput.value,42);assert.deepEqual(state.draftErrors.value,[]);assert.deepEqual(writes,[0])
})
