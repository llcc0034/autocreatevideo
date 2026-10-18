import assert from 'node:assert/strict'
import test,{type TestContext} from 'node:test'
import {createServer} from 'vite'
import {readFile} from 'node:fs/promises'
import {integerConstraintsFixture} from './fixtures/textConstraints.ts'

const operation='prompt.draft',parameter='seed'
const input=(value:string,badInput=false)=>({target:{value,validity:{badInput}}})
async function editor(context:TestContext){
  const vite=await createServer({logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}});context.after(()=>vite.close())
  const {default:Editor}=await vite.ssrLoadModule('/src/components/ModelTextExecutionEditor.vue')
  const {createRenderer,h,reactive,nextTick,ssrContextKey}=await import('vue')
  const catalog=integerConstraintsFixture()
  const options={schemaVersion:1,integerConstraints:{valueFields:catalog.value_fields,seedPolicies:catalog.seed_policies},operations:[{value:operation,label:'提示词起草'}],parameters:[{value:parameter,label:'随机种子',description:'测试合同',required:false,requireMaximum:false,template:{type:'integer',required:false}}]}
  const props:any=reactive({capabilities:[operation],options,disabled:false,modelValue:{schema_version:1,operations:{[operation]:{parameters:{[parameter]:{type:'integer',required:false,minimum:0,maximum:20,enum:[0,4,8],alignments:[{modulus:2,remainder:0},{modulus:4,remainder:0}]}},chat_request_options:{temperature:0}}}}})
  let state:any,updates=0,validity:string[]=[]
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Editor.setup(props,{expose(){},emit(event:string,value:any){if(event==='update:modelValue'){props.modelValue=value;updates++}else if(event==='validity')validity=value}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount());await nextTick()
  return {state,props,nextTick,updates:()=>updates,validity:()=>validity,rule:()=>props.modelValue.operations[operation].parameters[parameter]}
}

test('text allowed values and alignment drafts preserve the contract and remain invalid after unrelated edits',async context=>{
  const e=await editor(context),s=e.state,before=e.updates()
  s.listInput(operation,parameter,'enum',1,'',input('',true))
  s.listInput(operation,parameter,'alignments',1,'modulus',input('1e999'))
  await e.nextTick()
  assert.equal(e.updates(),before);assert.deepEqual(e.rule().enum,[0,4,8]);assert.equal(e.rule().alignments[1].modulus,4)
  assert.equal(e.validity().length,2)
  s.constraint(operation,parameter,'required',true);await e.nextTick()
  assert.equal(e.validity().length,2)
  s.numbers.discard(s.listKey(operation,parameter,'enum',1,''));await e.nextTick()
  assert.equal(e.validity().length,1);assert.equal(e.rule().enum[1],4)
  s.listInput(operation,parameter,'alignments',1,'modulus',input('4'));await e.nextTick()
  assert.deepEqual(e.validity(),[])
})

test('removing an earlier list row moves only its surviving drafts; explicit deletion clears only the removed row',async context=>{
  const e=await editor(context),s=e.state
  s.listInput(operation,parameter,'enum',1,'',input('',true));s.listInput(operation,parameter,'enum',2,'',input('-',true))
  s.listInput(operation,parameter,'alignments',1,'remainder',input('',true));await e.nextTick()
  s.removeItem(operation,parameter,'enum',0);await e.nextTick()
  assert.deepEqual(e.rule().enum,[4,8]);assert.equal(e.validity().length,3)
  assert.equal(s.numbers.value(s.listKey(operation,parameter,'enum',0,''),4),'')
  assert.equal(s.numbers.value(s.listKey(operation,parameter,'enum',1,''),8),'-')
  assert.equal(s.numbers.invalid(s.listKey(operation,parameter,'enum',2,'')),false)
  s.removeItem(operation,parameter,'enum',0);await e.nextTick()
  assert.deepEqual(e.rule().enum,[8]);assert.equal(e.validity().length,2)
  assert.equal(s.numbers.value(s.listKey(operation,parameter,'enum',0,''),8),'-')
  s.removeItem(operation,parameter,'alignments',0);await e.nextTick()
  assert.equal(s.numbers.invalid(s.listKey(operation,parameter,'alignments',0,'remainder')),true)
  s.numbers.discard(s.listKey(operation,parameter,'enum',0,''));s.numbers.discard(s.listKey(operation,parameter,'alignments',0,'remainder'));await e.nextTick()
  assert.deepEqual(e.validity(),[])
})

test('all text numeric controls use the same guard; clear and zero keep exact contract semantics',async context=>{
  const e=await editor(context),s=e.state
  s.numberInput(operation,parameter,'maximum',input('Infinity'));await e.nextTick()
  assert.equal(e.rule().maximum,20);assert.equal(e.validity().length,1)
  s.numbers.discard(s.numberKey(operation,parameter,'maximum'));await e.nextTick();assert.deepEqual(e.validity(),[])
  s.listInput(operation,parameter,'enum',0,'',input(''));await e.nextTick()
  assert.equal(e.rule().enum[0],null);assert.ok(e.validity().length)
  s.listInput(operation,parameter,'enum',0,'',input('0'));await e.nextTick();assert.deepEqual(e.validity(),[])
  s.listInput(operation,parameter,'alignments',0,'modulus',input('0'));await e.nextTick();assert.ok(e.validity().length)
  s.listInput(operation,parameter,'alignments',0,'modulus',input('2'));await e.nextTick();assert.deepEqual(e.validity(),[])
  s.numberInput(operation,parameter,'maximum',input(''));await e.nextTick()
  assert.equal(Object.hasOwn(e.rule(),'maximum'),false);assert.deepEqual(e.validity(),[])
})

test('disabled, stale indexes and missing catalogs cannot write; removed parameters do not regain stale drafts',async context=>{
  const e=await editor(context),s=e.state,id=s.listKey(operation,parameter,'enum',1,'')
  s.listInput(operation,parameter,'enum',1,'',input('',true));await e.nextTick()
  const before=e.updates();e.props.disabled=true
  s.listInput(operation,parameter,'enum',1,'',input('12'));s.removeItem(operation,parameter,'enum',0);s.numbers.discard(id);await e.nextTick()
  assert.equal(e.updates(),before);assert.equal(s.numbers.invalid(id),true)
  e.props.disabled=false
  s.listInput(operation,parameter,'enum',-1,'',input('12'));s.listInput(operation,parameter,'enum',99,'',input('12'))
  s.listInput(operation,parameter,'alignments',0,'unknown',input('1'));s.numberInput(operation,'unknown','maximum',input('1'))
  s.removeItem(operation,parameter,'enum',99);assert.equal(e.updates(),before)
  s.removeParameter(operation,parameter);s.addParameter(operation,parameter);await e.nextTick()
  assert.deepEqual(e.validity(),[]);assert.equal(s.numbers.invalid(id),false)
  const count=e.updates();e.props.options=null;s.numberInput(operation,parameter,'maximum',input('5'));await e.nextTick()
  assert.equal(e.updates(),count);assert.ok(e.validity().length)
})

test('list and scalar inputs expose individual accessible errors and explicit undo controls',async context=>{
  const e=await editor(context),source=await readFile(new URL('../src/components/ModelTextExecutionEditor.vue',import.meta.url),'utf8')
  assert.match(source,/:aria-invalid="numbers.invalid/);assert.match(source,/@click="numbers.discard/)
  assert.match(source,/:aria-label="`\$\{operationLabel\(operation\)\} \$\{parameterLabel\(key\)\} 允许值/)
  assert.doesNotMatch(source,/const numericErrors|const numericDrafts|numberOrEmpty/)
  assert.match(source,/label\{grid-template-columns:minmax\(0,1fr\)\}/)
  assert.match(source,/label>\.button\{min-width:0;max-width:100%;min-height:36px;white-space:normal\}/)
  assert.deepEqual(e.validity(),[])
})
