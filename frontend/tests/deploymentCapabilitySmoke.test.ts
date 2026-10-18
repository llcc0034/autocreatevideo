import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { readFile } from 'node:fs/promises'
import { createMemoryHistory, createRouter } from 'vue-router'

const setup = async (context:any) => {
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',logLevel:'silent',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  return vite
}
const snapshot = (operation='script.generate') => ({id:'snap',probeKind:'smoke',status:'passed',operations:[operation],targetMatches:true,fresh:true,capturedAt:'2026-09-05T10:00:00Z',expiresAt:'2099-09-05T11:00:00Z'})
const op = (operation='script.generate',evidenceState='fresh') => ({operation,promptProfiles:[],applicable:true,evidenceState,latestSnapshot:snapshot(operation),freshPassedSnapshot:evidenceState==='fresh'?snapshot(operation):null,latestAttempt:null,action:{enabled:true}})
const row = (operations:any[]) => ({subject:'deployment',deployment:{id:'d',providerDriver:'deepseek_api',targetState:'current',profileContractState:'current'},deploymentSmoke:{applicable:true,reasonCode:'ready',operations}})
const renderPanel = async (Panel:any, props:any) => {
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{path:'/workflows',name:'workflows',component:{render:()=>null}}]})
  await router.push('/'); await router.isReady()
  return renderToString(createSSRApp({render:()=>h(Panel,props)}).use(router))
}

test('per-capability signals preserve server decisions and expire without a page reload',async context=>{
  const vite=await setup(context)
  const {deploymentCapabilitySignal:signal}=await vite.ssrLoadModule('/src/services/deploymentCapabilitySmoke.ts')
  const current=row([op()]); const before=Date.parse('2026-09-05T10:30:00Z')
  assert.equal(signal(current,op(),before).label,'通过且有效')
  assert.equal(signal(current,op(),Date.parse(snapshot().expiresAt)).label,'已过期')
  assert.equal(signal(current,{...op(),latestAttempt:{status:'queued'}},before).label,'通过且有效','queued retry is separate from a still valid snapshot')
  assert.equal(signal(current,{...op(),latestAttempt:{status:'failed'}},before).label,'通过且有效','failed retry does not rewrite the previous immutable pass')
  for (const [state,label] of Object.entries({expired:'已过期',drifted:'配置已变更',failed:'烟测失败',missing:'未烟测',unverified:'暂无有效证据',unavailable:'无法核验',uncertain:'提交待确认',not_applicable:'当前不适用',unknown:'无法核验'})) {
    assert.equal(signal(current,op('script.generate',state),before).label,label)
  }
  assert.equal(signal(current,{...op(),latestAttempt:{resolutionRequired:true}},before).label,'提交待确认')
  for(const bad of [null,{...snapshot(),fresh:false},{...snapshot(),expiresAt:'bad'},{...snapshot(),targetMatches:false},snapshot('other')]) {
    assert.equal(signal(current,{...op(),freshPassedSnapshot:bad},before).label,'无法核验')
  }
  assert.equal(signal({...current,deployment:{...current.deployment,targetState:'invalid'}},op(),before).label,'无法核验')
})

test('all capabilities, expiry times and latest attempts are visible without conflating evidence',async context=>{
  const vite=await setup(context)
  const {default:Panel}=await vite.ssrLoadModule('/src/components/DeploymentCapabilitySmoke.vue')
  const operations=[op(),{...op('prompt.normalize','expired'),latestSnapshot:{...snapshot('prompt.normalize'),fresh:false,expiresAt:'2026-09-01T11:00:00Z'}}, {...op('script.structure','missing'),latestSnapshot:null},op('visual_rules.generate','failed')]
  operations[0]={...operations[0],latestAttempt:{status:'queued',createdAt:'2026-09-05T10:40:00Z',finishedAt:null}} as any
  const render=(props:any={})=>renderPanel(Panel,{row:row(operations),...props})
  const html=await render()
  assert.match(html,/有效通过 1 \/ 4 项/)
  for(const operation of operations) assert.ok(html.includes(operation.operation))
  for(const label of ['通过且有效','已过期','未烟测','烟测失败','最近任务','排队中','证据时间','到期时间']) assert.ok(html.includes(label),label)
  assert.match(html,/（已过期）/)
  assert.match(html,/2026-09-01T11:00:00Z/)
  assert.match(html,/重新读取能力烟测/)
  const failed=await render({error:'断开连接'})
  assert.match(failed,/烟测读取失败/);assert.doesNotMatch(failed,/通过且有效|有效通过/)
  assert.match(await render({loading:true}),/正在读取各能力/)
  assert.match(await render({row:null}),/不代表没有烟测记录/)
  assert.match(await render({row:row([])}),/未声明可直接烟测/)
})

test('both entry pages share the projection; refresh and deadline handling have no execution side effects',async()=>{
  const panel=await readFile(new URL('../src/components/DeploymentCapabilitySmoke.vue',import.meta.url),'utf8')
  const settings=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  const summary=await readFile(new URL('../src/components/ModelDeploymentExecutionSummary.vue',import.meta.url),'utf8')
  const center=await readFile(new URL('../src/views/AcceptanceSmokeCenterView.vue',import.meta.url),'utf8')
  assert.match(panel,/setTimeout\(tick/);assert.match(panel,/onBeforeUnmount/)
  assert.doesNotMatch(panel,/smokeTestDeployment|platformApi|capability_ttl_seconds/)
  assert.match(summary,/<DeploymentCapabilitySmoke v-else/)
  assert.match(center,/<DeploymentCapabilitySmoke v-else/)
  assert.doesNotMatch(settings,/modelDeploymentDirectSmokeStatus|deploymentEvidenceStatus/)
  const polling=settings.slice(settings.indexOf('const refreshDeploymentSmokeAttempts'),settings.indexOf('const cancelDeploymentSmoke'))
  assert.match(polling,/await loadDeploymentAcceptance\(\)/)
  const loading=settings.slice(settings.indexOf('const loadDeploymentAcceptance'),settings.indexOf('const loadProductionReadiness'))
  assert.match(loading,/listAcceptanceSmokeDeploymentRows/)
  assert.doesNotMatch(polling+loading,/smokeTestDeployment|createExecution|submitExecution/)
})

test('one successful strategy does not mark other strategies passed',async context=>{
  const vite=await setup(context)
  const {default:Panel}=await vite.ssrLoadModule('/src/components/DeploymentCapabilitySmoke.vue')
  const operation={...op(),promptProfiles:[
    {promptProfile:{id:'a',name:'已测试策略',version:1,execution_mode:''},evidenceState:'fresh',latestSnapshot:snapshot(),freshPassedSnapshot:snapshot()},
    {promptProfile:{id:'b',name:'未测试策略',version:2,execution_mode:''},evidenceState:'missing',latestSnapshot:null,freshPassedSnapshot:null},
  ]}
  const html=await renderPanel(Panel,{row:row([operation])})
  assert.match(html,/已测试策略 · v1/); assert.match(html,/未测试策略 · v2/)
  assert.match(html,/未烟测/); assert.match(html,/不代表全部策略通过/)
  const settings=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  assert.ok(settings.includes('ResourceSmokeCreateForm'))
  const selection=await readFile(new URL('../src/components/ResourceSmokeCreateForm.vue',import.meta.url),'utf8')
  assert.ok(selection.includes('请选择精确版本') && selection.includes('prompt_profile_version_id'))
  assert.ok(!settings.includes('actionSmokeBindingId'))
})

test('latest-attempt links use the exact original task and never snapshot, deployment or strategy identifiers',async context=>{
  const vite=await setup(context)
  const {default:Panel}=await vite.ssrLoadModule('/src/components/DeploymentCapabilitySmoke.vue')
  const attemptId='d3fa6b7e-5fa9-49d6-9dfd-b9452b709273'
  const operation={...op(),latestAttempt:{id:attemptId,status:'failed',createdAt:'2026-09-05T10:40:00Z',finishedAt:'2026-09-05T10:41:00Z',errorCode:'output_invalid'}}
  const html=await renderPanel(Panel,{row:row([operation])})
  assert.match(html,new RegExp(`href="/workflows\\?category=smoke&amp;source_id=${attemptId}"`))
  assert.ok(html.includes(`任务标识：${attemptId}`))
  assert.match(html,/任务诊断码：output_invalid/)
  assert.match(html,/完成于/)
  assert.match(html,/与上方验收快照不是同一次/)
  assert.match(html,/不新建或重跑/)
  assert.doesNotMatch(html,/source_id=(snap|d|a)(?:"|&)/)
  for(const status of ['preparing','queued','submitting','submitted','cancel_requested','uncertain','cancelled','succeeded']){
    const rendered=await renderPanel(Panel,{row:row([{...operation,latestAttempt:{...operation.latestAttempt,status,finishedAt:null,errorCode:''}}])})
    assert.match(rendered,new RegExp(`source_id=${attemptId}`),status)
    assert.match(rendered,/创建于/)
    assert.doesNotMatch(rendered,/任务诊断码/)
  }
  for(const props of [{row:row([op()])},{row:row([operation]),loading:true},{row:row([operation]),error:'读取失败'},{row:null}]){
    assert.doesNotMatch(await renderPanel(Panel,props),/href="\/workflows/)
  }
  const missing=await renderPanel(Panel,{row:row([{...operation,latestAttempt:{...operation.latestAttempt,id:''}}])})
  assert.match(missing,/记录缺少标识，无法定位/)
  assert.doesNotMatch(missing,/href="\/workflows/)
  const source=await readFile(new URL('../src/components/DeploymentCapabilitySmoke.vue',import.meta.url),'utf8')
  assert.doesNotMatch(source,/platformApi|smokeTestDeployment|submitExecution|createExecution/)
})
