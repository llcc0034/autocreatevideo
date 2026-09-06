import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'

const start = () => createServer({root:fileURLToPath(new URL('..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
const id = '00000000-0000-4000-8000-000000000001'
const other = '00000000-0000-4000-8000-000000000002'
const projector = {id:'p1',projectorKey:'generic.example',version:1,definitionSha256:'a'.repeat(64)}
const model = (id='m1') => ({id,profileKey:'configured-model',version:2,status:'published',specification:{capabilities:['video.reference_to_video'],conditioning_contract:{modes:[{key:'Refs',input_kind:'classified_references',inputs:{entity_images:{minimum_items:0,maximum_items:99}}}]}}})
const workflow = (workflowId=id, modelId='m1') => ({id:workflowId,templateName:'已发布工作流',version:1,modelProfileVersion:modelId,inputContractSha256:'b'.repeat(64),inputContract:{contract_projector:{id:projector.projectorKey,version_id:projector.id,version:1,definition_sha256:projector.definitionSha256},classified_references:{groups:{images:{min_items:0,max_items:9}}}}})
const flush = async () => { for(let i=0;i<5;i++){await Promise.resolve();await nextTick()} }
function mount(setup:()=>unknown) {
  const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert:()=>{},remove:()=>{},setText:()=>{},setElementText:()=>{},parentNode:()=>null,nextSibling:()=>null,patchProp:()=>{}})
  let result:any
  const app=renderer.createApp({setup(){result=setup();return()=>h('div')}})
  app.mount({});return {app,result}
}

test('context is lazy outside a workflow and reads its exact model, never a URL-selected model',async t=>{
  const vite=await start();t.after(()=>vite.close())
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const calls:string[]=[]
  t.mock.method(platformApi,'getWorkflowVersionForAudit',async(value:string)=>{calls.push(value);return workflow(value)})
  t.mock.method(platformApi,'getModelProfile',async(value:string)=>{calls.push(value);return model(value)})
  const {useWorkflowProjectorContext}=await vite.ssrLoadModule('/src/services/workflowProjectorContext.ts')
  const input=reactive({workflowId:undefined as string|undefined,projector})
  const {app,result}=mount(()=>useWorkflowProjectorContext(()=>input));t.after(()=>app.unmount())
  await flush();assert.equal(result.context.value.state,'none');assert.deepEqual(calls,[])
  input.workflowId=id;await flush()
  assert.equal(result.context.value.state,'ready');assert.deepEqual(calls,[id,'m1'])
  assert.equal(result.context.value.model.id,'m1')
})

test('invalid IDs, mismatched projector SHA, unbound models, permissions and model identity fail closed with retry',async t=>{
  const vite=await start();t.after(()=>vite.close())
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  let current:any=workflow();let failure=false;let modelIdentity='m1';let modelReads=0
  t.mock.method(platformApi,'getWorkflowVersionForAudit',async()=>{if(failure)throw Error('无权限');return current})
  t.mock.method(platformApi,'getModelProfile',async()=>{modelReads++;return model(modelIdentity)})
  const {useWorkflowProjectorContext}=await vite.ssrLoadModule('/src/services/workflowProjectorContext.ts')
  const input=reactive({workflowId:'invalid',projector})
  const {app,result}=mount(()=>useWorkflowProjectorContext(()=>input));t.after(()=>app.unmount())
  await flush();assert.equal(result.context.value.state,'error')
  current.inputContract.contract_projector.definition_sha256='wrong';input.workflowId=id;await flush()
  assert.match(result.context.value.message,/冻结 Projector 引用/);assert.equal(modelReads,0)
  current=workflow();current.modelProfileVersion='';await result.reload()
  assert.match(result.context.value.message,/尚未绑定/);assert.equal(modelReads,0)
  current=workflow();modelIdentity='wrong';await result.reload()
  assert.match(result.context.value.message,/模型版本与工作流绑定不一致/)
  failure=true;await result.reload();assert.match(result.context.value.message,/无权限/)
  failure=false;modelIdentity='m1';await result.reload();assert.equal(result.context.value.state,'ready')
})

test('route switches, closing and late responses cannot restore another workflow context',async t=>{
  const vite=await start();t.after(()=>vite.close())
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const pending=new Map<string,(value:any)=>void>()
  t.mock.method(platformApi,'getWorkflowVersionForAudit',(value:string)=>new Promise(resolve=>pending.set(value,resolve)))
  t.mock.method(platformApi,'getModelProfile',async(value:string)=>model(value))
  const {useWorkflowProjectorContext}=await vite.ssrLoadModule('/src/services/workflowProjectorContext.ts')
  const input=reactive({workflowId:id as string|undefined,projector})
  const {app,result}=mount(()=>useWorkflowProjectorContext(()=>input));t.after(()=>app.unmount())
  input.workflowId=other;await flush();pending.get(other)!(workflow(other,'m2'));await flush()
  assert.equal(result.context.value.model.id,'m2')
  pending.get(id)!(workflow());await flush();assert.equal(result.context.value.model.id,'m2')
  const reload=result.reload();input.workflowId=undefined;await flush()
  pending.get(other)!(workflow(other,'m2'));await reload;assert.equal(result.context.value.state,'none')
})

test('frozen ranges use workflow data, not larger current model counts, and never fill missing data',async t=>{
  const vite=await start();t.after(()=>vite.close())
  const {frozenAssetQuantity}=await vite.ssrLoadModule('/src/services/workflowProjectorContext.ts')
  const ctx:any={state:'ready',workflow:workflow(),model:model()}
  const before=JSON.stringify(ctx)
  assert.equal(frozenAssetQuantity(ctx,'images').maximum,9)
  assert.equal(JSON.stringify(ctx),before)
  assert.equal(frozenAssetQuantity(ctx,'missing'),null)
  ctx.workflow.inputContract.classified_references.groups.images.max_items=null
  assert.equal(frozenAssetQuantity(ctx,'images'),null)
  delete ctx.workflow.inputContract.classified_references
  ctx.workflow.inputContract.asset_groups=[{key:'references',min_items:1,max_items:3}]
  assert.equal(frozenAssetQuantity(ctx,'references').minimum,1)
})

test('model comparisons distinguish not loaded, loading, failure, match and mismatch without mutation',async t=>{
  const vite=await start();t.after(()=>vite.close())
  const {projectorModelComparison:compare}=await vite.ssrLoadModule('/src/services/workflowProjectorContext.ts')
  const config={capability:'video.reference_to_video',conditioning_mode:'Refs'}, before=JSON.stringify(config)
  assert.equal(compare(config,null).state,'none')
  assert.equal(compare(config,null,true).state,'loading')
  assert.equal(compare(config,null,false,'403').state,'error')
  assert.equal(compare(config,model()).state,'matched')
  assert.equal(compare({...config,conditioning_mode:'Unknown'},model()).state,'mismatch')
  assert.equal(JSON.stringify(config),before)
})

test('readonly classified catalog has plain published values and one optional comparison entry, not warnings',async t=>{
  const vite=await start();t.after(()=>vite.close())
  const {default:Editor}=await vite.ssrLoadModule('/src/components/WorkflowClassifiedReferenceEditor.vue')
  const definition={node_roles:{},asset_slots:[],projection:{config:{capability:'video.reference_to_video',conditioning_mode:'Refs',dynamic_groups:{},static_semantics:{}}}}
  const before=JSON.stringify(definition)
  const html=await renderToString(createSSRApp({render:()=>h(Editor,{modelValue:definition,options:{},readOnly:true,disabled:true})}))
  assert.match(html,/选择模型进行对照（可选）/)
  assert.match(html,/<output[^>]*>video.reference_to_video<\/output>/)
  assert.doesNotMatch(html,/尚未核对|请选择对照版本|读取模型能力版本/)
  assert.equal(JSON.stringify(definition),before)
})

test('readonly source shows frozen quantity and exact model without a model selector or extra fetch',async t=>{
  const vite=await start();t.after(()=>vite.close())
  const {platformApi}=await vite.ssrLoadModule('/src/services/platformApi.ts')
  const api=t.mock.method(platformApi,'getModelProfile',async()=>{throw Error('unexpected fetch')})
  const {default:Source}=await vite.ssrLoadModule('/src/components/WorkflowAssetContractSource.vue')
  const wrapper={...Source,setup(props:any,ctx:any){const s=Source.setup(props,ctx);s.expanded.value=true;return s}}
  const app=createSSRApp(wrapper,{definition:{},slot:{key:'images',contract_source:'model_profile'},readOnly:true,workflowContext:{state:'ready',workflow:workflow(),model:model()}})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/settings');await router.isReady();app.use(router)
  const html=await renderToString(app)
  assert.match(html,/0–9 份/);assert.match(html,/configured-model v2/)
  assert.doesNotMatch(html,/<select|0–99|未加载对照模型/)
  assert.equal(api.mock.callCount(),0)
})
