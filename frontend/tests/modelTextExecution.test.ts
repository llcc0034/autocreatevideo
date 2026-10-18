import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { integerConstraintsFixture } from './fixtures/textConstraints.ts'

const catalog=integerConstraintsFixture()
const options={schemaVersion:1,integerConstraints:{valueFields:catalog.value_fields,seedPolicies:catalog.seed_policies},operations:[{value:'prompt.draft',label:'提示词起草'},{value:'prompt.fields.generate',label:'字段生成'}],parameters:[
  {value:'seed',label:'随机种子',description:'按模型能力添加',required:false,requireMaximum:false,template:{type:'integer',required:false}},
]}
const caps=['prompt.draft']
async function setup(context:any) {
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  return vite
}

test('generic structured text policy uses the same registered model editor without native policy',async context=>{
  const vite=await setup(context),service=await vite.ssrLoadModule('/src/services/modelTextExecution.ts')
  const extended={...options,operations:[...options.operations,{value:'text.structured',label:'通用结构化文本'}]}
  const policy=service.initializeTextExecution(undefined,['text.structured'],extended)
  assert.deepEqual(Object.keys(policy.operations),['text.structured'])
  assert.deepEqual(service.textExecutionErrors(policy,['text.structured'],extended),[])
  assert.deepEqual(policy.operations['text.structured'].parameters,{})
  assert.equal('native_policy' in policy,false)
})
test('text contracts initialize only registered selections, preserve data and never guess budgets',async context=>{
  const vite=await setup(context), service=await vite.ssrLoadModule('/src/services/modelTextExecution.ts')
  const blank=service.initializeTextExecution(undefined,caps,options)
  assert.deepEqual(blank.operations['prompt.draft'].parameters,{})
  assert.deepEqual(service.textExecutionErrors(blank,caps,options),[])
  const added=structuredClone(blank)
  added.operations['prompt.draft'].parameters.seed={type:'integer',required:false,minimum:0}
  const ready=service.updateTextExecutionParameter(added,'prompt.draft','seed','maximum','800')
  assert.deepEqual(service.textExecutionErrors(ready,caps,options),[])
  ready.operations['prompt.draft'].chat_request_options={temperature:0,thinking:{type:'disabled'},extras:[true,'unchanged']}
  const extended=service.initializeTextExecution(ready,[...caps,'prompt.fields.generate'],options)
  assert.deepEqual(extended.operations['prompt.draft'],ready.operations['prompt.draft'])
  assert.deepEqual(blank.operations['prompt.draft'].parameters,{})
  const cleared=service.updateTextExecutionParameter(ready,'prompt.draft','seed','maximum','')
  assert.equal(Object.hasOwn(cleared.operations['prompt.draft'].parameters.seed,'maximum'),false)
  assert.ok(service.textExecutionErrors(ready,[],options).some((error:string)=>error.includes('未在模型能力中')))
  assert.ok(service.textExecutionErrors(ready,caps,null).length)
  for (const change of [{minimum:801},{default:900},{maximum:true},{required:null},{enum:[]},{enum:[0,0]},{enum:[false]},{enum:[801]},
    {alignments:[{modulus:0,remainder:0}]},{alignments:[{modulus:2,remainder:2}]},{alignments:[{modulus:2,remainder:0}],default:1},
    {const:2,default:1},{enum:[2],const:1},{seed_policy:'other'},{seed_policy:'generate_once',default:0},{min_length:0}]) {
    const broken=structuredClone(ready);Object.assign(broken.operations['prompt.draft'].parameters.seed,change)
    assert.ok(service.textExecutionErrors(broken,caps,options).length)
  }
  assert.throws(()=>service.initializeTextExecution({schema_version:99},caps,options))
})

test('text editor renders typed controls, preserves request options, and blocks readonly mutations',async context=>{
  const vite=await setup(context), {default:Editor}=await vite.ssrLoadModule('/src/components/ModelTextExecutionEditor.vue')
  const {createSSRApp,h,reactive}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props=reactive({modelValue:undefined as any,capabilities:caps,options,disabled:false})
  let state:any,updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:unknown)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}}});return()=>h('div')}}))
  state.initialize();state.addParameter('prompt.draft','seed');state.parameter('prompt.draft','seed','maximum','1000')
  assert.deepEqual(state.errors.value,[])
  state.policy('prompt.draft',{...props.modelValue.operations['prompt.draft'],chat_request_options:{temperature:0.3,thinking:{type:'disabled'}}})
  state.parameter('prompt.draft','seed','default','200')
  assert.equal(props.modelValue.operations['prompt.draft'].chat_request_options.thinking.type,'disabled')
  const html=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  assert.match(html,/文本执行协议与参数范围/);assert.match(html,/type="number"/);assert.match(html,/请求选项/)
  assert.match(html,/固定值（可选）/);assert.match(html,/添加允许值/);assert.match(html,/添加对齐规则/);assert.doesNotMatch(html,/&quot;thinking&quot;/)
  state.constraint('prompt.draft','seed','enum',[200,null]);assert.ok(state.errors.value.length)
  state.listItem('prompt.draft','seed','enum',1,400);assert.deepEqual(state.errors.value,[])
  state.constraint('prompt.draft','seed','alignments',[{modulus:200,remainder:0}]);assert.deepEqual(state.errors.value,[])
  state.constraint('prompt.draft','seed','seed_policy','generate_once');assert.ok(state.errors.value.length)
  assert.equal(props.modelValue.operations['prompt.draft'].parameters.seed.default,200)
  state.parameter('prompt.draft','seed','default','');assert.deepEqual(state.errors.value,[])
  state.removeItem('prompt.draft','seed','enum',1);assert.deepEqual(props.modelValue.operations['prompt.draft'].parameters.seed.enum,[200])
  state.numberInput('prompt.draft','seed','maximum',{target:{value:'',validity:{badInput:true}}})
  assert.ok(state.errors.value.some((value:string)=>value.includes('数字尚未完成')))
  assert.equal(props.modelValue.operations['prompt.draft'].parameters.seed.maximum,1000)
  assert.equal(state.numericValue('prompt.draft','seed','maximum',props.modelValue.operations['prompt.draft'].parameters.seed),'')
  state.constraint('prompt.draft','seed','required',true)
  assert.ok(state.errors.value.some((value:string)=>value.includes('数字尚未完成')))
  state.numberInput('prompt.draft','seed','maximum',{target:{value:'800',validity:{badInput:false}}})
  assert.deepEqual(state.errors.value,[])
  assert.equal(props.modelValue.operations['prompt.draft'].parameters.seed.maximum,800)
  assert.equal(state.numericValue('prompt.draft','seed','maximum',props.modelValue.operations['prompt.draft'].parameters.seed),800)
  props.disabled=true;const before=updates
  state.initialize();state.parameter('prompt.draft','seed','maximum','9');state.removeParameter('prompt.draft','seed')
  state.constraint('prompt.draft','seed','const',0);state.listItem('prompt.draft','seed','enum',0,600);state.removeItem('prompt.draft','seed','enum',0)
  assert.equal(updates,before)
  const readonly=await renderToString(createSSRApp({render:()=>h(Editor,props)}))
  for(const tag of readonly.match(/<(?:input|select|textarea)\b[^>]*>/g)||[])assert.match(tag,/disabled/)
})

test('integer constraints preserve zero, types and unknown data; JSON cannot rewrite parameter contracts',async context=>{
  const vite=await setup(context),service=await vite.ssrLoadModule('/src/services/modelTextExecution.ts')
  let contract=service.initializeTextExecution(undefined,caps,options)
  contract.operations['prompt.draft'].parameters.seed={type:'integer',required:true,enum:[0,2],const:0,default:0,alignments:[{modulus:2,remainder:0}]}
  assert.deepEqual(service.textExecutionErrors(contract,caps,options),[])
  const before=structuredClone(contract)
  const changed=service.setTextExecutionConstraint(contract,'prompt.draft','seed','required',false)
  assert.equal(contract.operations['prompt.draft'].parameters.seed.required,true)
  assert.equal(changed.operations['prompt.draft'].parameters.seed.const,0)
  const spec={text_execution:before,custom:{unchanged:true}}
  service.assertTextParametersUnchanged(spec,{custom:{changed:true},text_execution:structuredClone(before)})
  const rearranged=structuredClone(spec);rearranged.text_execution.operations['prompt.draft'].parameters.seed={...Object.fromEntries(Object.entries(before.operations['prompt.draft'].parameters.seed).reverse())}
  service.assertTextParametersUnchanged(spec,rearranged)
  for(const next of [{}, {text_execution:changed}])assert.throws(()=>service.assertTextParametersUnchanged(spec,next),/完整 JSON/)
  contract.operations['prompt.draft'].parameters.seed={type:'integer',required:false,seed_policy:'generate_once'}
  assert.ok(service.textExecutionErrors(contract,caps,options).some((error:string)=>error.includes('有限范围')))
  contract.operations['prompt.draft'].parameters=[]
  assert.ok(service.textExecutionErrors(contract,caps,options).some((error:string)=>error.includes('参数必须是分组')))
})

test('structured options preserve JSON types, reject duplicate keys, and keep invalid numeric edits visibly blocked',async context=>{
  const vite=await setup(context), {default:Editor}=await vite.ssrLoadModule('/src/components/StructuredJsonEditor.vue')
  const {createSSRApp,h,reactive}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props=reactive({modelValue:{temperature:0,thinking:{enabled:false},items:[null,'name']} as any,label:'请求选项',disabled:false,fixedObject:true})
  let state:any,updates=0
  await renderToString(createSSRApp({setup(){state=Editor.setup(props,{expose:()=>{},emit:(event:string,value:unknown)=>{if(event==='update:modelValue'){props.modelValue=value;updates++}}});return()=>h('div')}}))
  state.keyDraft.value='temperature';state.add();assert.match(state.ownError.value,/重复/)
  const before=updates;state.changeType('array');assert.equal(updates,before)
  state.keyDraft.value='custom';state.add();state.child('custom',false)
  assert.equal(props.modelValue.custom,false);assert.equal(props.modelValue.temperature,0)
  assert.deepEqual(props.modelValue.items,[null,'name'])
  state.child('temperature',0.2);state.remove('custom');assert.equal(Object.hasOwn(props.modelValue,'custom'),false)
  props.disabled=true;const disabledBefore=updates;state.remove('temperature');state.child('temperature',8);state.add();assert.equal(updates,disabledBefore)
  props.disabled=false;props.fixedObject=false;props.modelValue=0.2
  state.number('');assert.match(state.ownError.value,/有效数字/);assert.equal(props.modelValue,0.2)
  state.number('0');assert.equal(props.modelValue,0);assert.equal(state.ownError.value,'')
})
