import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { readFile } from 'node:fs/promises'

const setup = async (context:any) => {
  const vite=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',logLevel:'silent',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  return vite
}
const snapshot = (operation='script.generate') => ({id:'snap',probeKind:'smoke',status:'passed',operations:[operation],targetMatches:true,fresh:true,capturedAt:'2026-09-05T10:00:00Z',expiresAt:'2099-09-05T11:00:00Z'})
const op = (operation='script.generate',evidenceState='fresh') => ({operation,promptProfiles:[],applicable:true,evidenceState,latestSnapshot:snapshot(operation),freshPassedSnapshot:evidenceState==='fresh'?snapshot(operation):null,latestAttempt:null,action:{enabled:true}})
const row = (operations:any[]) => ({subject:'deployment',deployment:{id:'d',providerDriver:'deepseek_api',targetState:'current',profileContractState:'current'},deploymentSmoke:{applicable:true,reasonCode:'ready',operations}})

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
  const render=(props:any={})=>renderToString(createSSRApp({render:()=>h(Panel,{row:row(operations),...props})}))
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
  assert.match(polling,/listAcceptanceSmokeDeploymentRows/)
})

test('one successful strategy does not mark other strategies passed',async context=>{
  const vite=await setup(context)
  const {default:Panel}=await vite.ssrLoadModule('/src/components/DeploymentCapabilitySmoke.vue')
  const operation={...op(),promptProfiles:[
    {promptProfile:{prompt_profile_binding_id:'a',prompt_profile_name:'已测试策略',prompt_profile_version:1},evidenceState:'fresh',latestSnapshot:snapshot(),freshPassedSnapshot:snapshot()},
    {promptProfile:{prompt_profile_binding_id:'b',prompt_profile_name:'未测试策略',prompt_profile_version:2},evidenceState:'missing',latestSnapshot:null,freshPassedSnapshot:null},
  ]}
  const html=await renderToString(createSSRApp({render:()=>h(Panel,{row:row([operation])})}))
  assert.match(html,/已测试策略 · v1/); assert.match(html,/未测试策略 · v2/)
  assert.match(html,/未烟测/); assert.match(html,/不代表全部策略通过/)
  const settings=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
  assert.match(settings,/v-model="actionSmokeBindingId"/)
  assert.match(settings,/请选择策略版本/)
  assert.match(settings,/!actionSmokeSelectionValid/)
})
