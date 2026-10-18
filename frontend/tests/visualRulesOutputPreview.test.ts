import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {visualRulesOutputRaw} from './visualRulesOutputFixture.ts'
async function setup(context:any){
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  return vite
}
const good=()=>({preview_only:true,response_schema_version:2,valid:true,
  candidate:{continuity_summary:'Full summary.',color_script:'Rendered colors.',negative_prompt:''},
  warnings:[] as string[],errors:[] as {code:string;message:string}[]})

test('preview adapter never treats missing, failed or non-preview evidence as valid',async context=>{
  const vite=await setup(context),{adaptVisualRulesOutputPreview:adapt}=await vite.ssrLoadModule('/src/services/visualRulesOutputPreview.ts')
  assert.equal(adapt(good()).previewOnly,true)
  const bad={...good(),valid:false,candidate:null,errors:[{code:'bad_enum',message:'不支持的枚举'}]}
  assert.equal(adapt(bad).valid,false)
  for(const raw of [undefined,{}, {...good(),preview_only:false},{...good(),candidate:null},
    {...good(),errors:bad.errors},{...bad,errors:[]},{...bad,candidate:good().candidate},
    {...good(),warnings:[5]},{...good(),candidate:{field:5}}])assert.throws(()=>adapt(raw))
})

test('preview preserves sample on errors, invalidates changed inputs, ignores stale results and does not save',async context=>{
  const vite=await setup(context),{api}=await vite.ssrLoadModule('/src/services/api.ts')
  const {adaptVisualRulesOutputOptions}=await vite.ssrLoadModule('/src/services/visualRulesOutput.ts')
  const {default:Preview}=await vite.ssrLoadModule('/src/components/VisualRulesOutputPreview.vue')
  const {createSSRApp,createRenderer,h,reactive,ssrContextKey}=await import('vue'),{renderToString}=await import('vue/server-renderer')
  const props=reactive({options:adaptVisualRulesOutputOptions(visualRulesOutputRaw),qualityAdvisories:undefined as any,allowedValues:undefined as any,disabled:false})
  let state:any,calls:any[]=[],reply=good(),pending:((value:unknown)=>void)|undefined
  api.post=async(path:string,payload:unknown)=>{calls.push({path,payload});return {data:reply}}
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app=renderer.createApp({setup(){state=Preview.setup(props,{expose:()=>{}});return()=>h('div')}})
  app.provide(ssrContextKey,{});app.mount({});context.after(()=>app.unmount())
  state.example()
  const sample=state.responseText.value
  assert.deepEqual(JSON.parse(sample),visualRulesOutputRaw.response_example)
  await state.validateSample()
  assert.equal(state.result.value.valid,true)
  assert.deepEqual(calls[0],{path:'/business-output-versions/preview-visual-rules-output/',payload:{response_text:sample}})
  props.qualityAdvisories={schema_version:1,continuity_summary_max_chars:1}
  assert.equal(state.result.value,null)
  reply={...good(),warnings:['保留完整正文，供人工审核']}
  await state.validateSample()
  assert.equal(state.result.value.warnings.length,1)
  assert.deepEqual(calls.at(-1).payload.quality_advisories,props.qualityAdvisories)
  props.allowedValues={lighting:['low_key']}
  assert.equal(state.result.value,null)
  await state.validateSample()
  assert.deepEqual(calls.at(-1).payload.allowed_values,props.allowedValues)
  api.post=async()=>{throw new Error('暂时不可用')}
  await state.validateSample()
  assert.equal(state.result.value,null)
  assert.equal(state.responseText.value,sample)
  assert.ok(state.error.value)
  api.post=()=>new Promise(resolve=>{pending=resolve})
  const running=state.validateSample()
  assert.equal(state.busy.value,true)
  state.responseText.value='changed while loading'
  await state.validateSample()
  pending!({data:good()});await running
  assert.equal(state.result.value,null)
  assert.equal(state.busy.value,false)
  props.disabled=true
  state.example();await state.validateSample()
  assert.equal(state.responseText.value,'changed while loading')
  const html=await renderToString(createSSRApp({render:()=>h(Preview,props)}))
  assert.match(html,/不调用模型/);assert.match(html,/不保存正文/);assert.match(html,/Smoke/);assert.match(html,/<textarea disabled/)
})
