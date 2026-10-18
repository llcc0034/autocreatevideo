import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'

const server = () => createServer({ server: { middlewareMode:true, hmr:false }, appType:'custom', logLevel:'silent', optimizeDeps:{noDiscovery:true} })
const workflow = (id='w1') => ({ subject:'workflow', workflow:{id,templateName:'参考生成',version:2,status:'published'},
  association:{deploymentId:'d1',validatedDeploymentMatches:true,hasSmokeAttempt:true},
  validation:{fresh:true,message:'校验有效'}, smoke:{fresh:true,state:'fresh',evidenceDeploymentId:'d1',message:'烟测有效',latestAttempt:null}, actions:[] })

test('deployment smoke signal counts exact workflows, not lifecycle or historical attempts', async context => {
  const vite = await server(); context.after(()=>vite.close())
  const {deploymentWorkflowSignal,workflowExecutionSignal,workflowExecutionUrl} = await vite.ssrLoadModule('/src/services/modelDeploymentExecution.ts')
  const row = { deployment:{targetState:'current',profileContractState:'current',status:'active'}, workflowAssociationSummary:{freshSmokeCount:1,unresolvedSmokeCount:0,smokeHistoryCount:9} }
  assert.equal(deploymentWorkflowSignal(row).label,'1 个工作流通过')
  assert.match(deploymentWorkflowSignal(row).description,/其他工作流仍需分别验收/)
  assert.equal(deploymentWorkflowSignal({...row,workflowAssociationSummary:{freshSmokeCount:0,unresolvedSmokeCount:0,smokeHistoryCount:9}}).label,'暂无有效通过')
  assert.equal(deploymentWorkflowSignal({...row,workflowAssociationSummary:{freshSmokeCount:1,unresolvedSmokeCount:1}}).label,'提交待确认')
  assert.equal(deploymentWorkflowSignal({...row,deployment:{targetState:'invalid'}}).label,'合同待处理')
  assert.equal(deploymentWorkflowSignal(null).label,'待读取')
  assert.equal(deploymentWorkflowSignal(row,'请求失败').label,'读取失败')
  const w = workflow()
  assert.equal(workflowExecutionSignal(w,'d1').label,'烟测通过')
  assert.notEqual(workflowExecutionSignal(w,'d2').label,'烟测通过')
  assert.equal(workflowExecutionSignal({...w,validation:{fresh:false,message:'目标漂移'}},'d1').label,'需重新验收')
  assert.equal(workflowExecutionSignal({...w,association:{validatedDeploymentMatches:false,hasSmokeAttempt:false}},'d1').label,'仅同模型候选')
  assert.equal(workflowExecutionSignal({...w,smoke:{...w.smoke,fresh:false,state:'failed'}},'d1').label,'烟测失败')
  assert.equal(workflowExecutionSignal({...w,smoke:{...w.smoke,latestAttempt:{resolutionRequired:true}}},'d1').label,'提交待确认')
  assert.match(workflowExecutionUrl(w,'d1'),/workflow_id=w1&deployment_id=d1/)
})

test('instance relation component paginates, ignores stale responses and exposes errors', async context => {
  const vite = await server(); context.after(()=>vite.close())
  const {platformApi} = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const original = platformApi.getAcceptanceSmokePage
  context.after(()=>{platformApi.getAcceptanceSmokePage=original})
  const pending:any[]=[]
  let freshCount = 1
  platformApi.getAcceptanceSmokePage=(query:any)=>query.subject==='deployment'
    ? Promise.resolve({count:1,next:null,results:[{subject:'deployment',deployment:{id:query.deploymentId,targetState:'current',profileContractState:'current'},workflowAssociationSummary:{freshSmokeCount:freshCount,unresolvedSmokeCount:0}}]})
    : new Promise((resolve,reject)=>pending.push({query,resolve,reject}))
  const {default:Component} = await vite.ssrLoadModule('/src/components/ModelDeploymentExecutionSummary.vue')
  const {createSSRApp,h,reactive}=await import('vue')
  const {renderToString}=await import('vue/server-renderer')
  const props=reactive({deployment:{id:'d1',providerDriver:'comfyui',updatedAt:''},profile:{id:'m1',specification:{model_family:'family'}},acceptance:null,bindings:[],bindingsLoading:false,bindingsError:''})
  let state:any
  await renderToString(createSSRApp({setup(){state=Component.setup(props,{expose:()=>{},emit:()=>{}});return()=>h('div')}}))
  assert.deepEqual(pending[0].query,{subject:'workflow',deploymentId:'d1',page:1,pageSize:5})
  const newer=state.refresh(2)
  pending[1].resolve({results:[workflow('w2')],count:6,next:null})
  await newer
  assert.equal(state.page.value,2);assert.equal(state.workflows.value[0].workflow.id,'w2')
  assert.equal(state.signal.value.label,'1 个工作流通过')
  pending[0].resolve({results:[workflow('stale')],count:6,next:'page2'})
  await Promise.resolve();await Promise.resolve()
  assert.equal(state.workflows.value[0].workflow.id,'w2')
  freshCount = 0
  const invalid=state.refresh()
  pending[2].resolve({results:[{...workflow(),association:{deploymentId:'other'}}],count:1,next:null})
  await invalid
  assert.match(state.error.value,/不匹配/);assert.deepEqual(state.workflows.value,[])
  const failed=state.refresh();pending[3].reject(new Error('断开连接'));await failed
  assert.ok(state.error.value);assert.equal(state.signal.value.label,'读取失败')
})

test('deployment display points to exact execution instances and never infers prompt bindings', async context => {
  const vite = await server(); context.after(()=>vite.close())
  const {default:Component}=await vite.ssrLoadModule('/src/components/ModelDeploymentExecutionSummary.vue')
  const {createSSRApp,h}=await import('vue');const {renderToString}=await import('vue/server-renderer')
  const {createRouter,createMemoryHistory}=await import('vue-router')
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/settings');await router.isReady()
  const app=createSSRApp({render:()=>h(Component,{deployment:{id:'d1',providerDriver:'async_rest'},acceptance:null})});app.use(router)
  const html=await renderToString(app)
  assert.match(html,/业务执行实例/);assert.match(html,/此处只核对部署资源/)
  assert.match(html,/kind=execution/);assert.match(html,/source_kind=deployment/)
  assert.match(html,/source_id=d1/);assert.match(html,/relation=executions/)
  assert.doesNotMatch(html,/适用提示词策略|模型族通用|运行时绑定/)
  const settings=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  assert.doesNotMatch(settings,/modelPromptContext|useModelPromptBindings|show-bindings/)
  const manager=await readFile(new URL('../src/components/PromptProfileSettings.vue',import.meta.url),'utf8')
  assert.doesNotMatch(manager,/bindingForm|listPromptProfileBindings|运行时绑定|modelContextRequested/)
  assert.match(manager,/发布 → 执行实例明确引用/)
  assert.match(manager,/useRegistryRelations\('prompt'/)
})
