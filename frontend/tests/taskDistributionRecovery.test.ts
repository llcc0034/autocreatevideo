import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { compile, createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { readFileSync } from 'node:fs'

const root = fileURLToPath(new URL('..', import.meta.url)), taskId = '11111111-1111-4111-8111-111111111111'
const time = '2026-09-12T08:00:00Z'
const phase = () => ({task_id:taskId,kind:'image',state:'failed',revision:1,requested_at:time,started_at:time,finished_at:time,error:'副本处理失败'})
const page = (row = phase()) => ({count:1,next:null,previous:null,results:[row]})

test('output card wraps actions without squeezing metadata or clipping download controls', () => {
  const css = readFileSync(new URL('../src/workflow-settings.css', import.meta.url), 'utf8')
  const declarations = (selector: string) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const blocks = [...css.matchAll(new RegExp(`${escaped}\\{([^}]+)\\}`, 'g'))]
    assert.ok(blocks.length, `Missing layout rule: ${selector}`)
    return Object.fromEntries(blocks.flatMap(match => match[1].split(';').filter(Boolean).map(value => {
      const colon = value.indexOf(':')
      return [value.slice(0, colon).trim(), value.slice(colon + 1).trim()]
    })))
  }
  assert.equal(declarations('.task-output-card>footer')['flex-wrap'], 'wrap')
  assert.equal(declarations('.task-output-card>footer>div:first-child').flex, '1 1 220px')
  assert.equal(declarations('.task-output-card>footer>div:first-child')['overflow-wrap'], 'anywhere')
  const actions = declarations('.task-output-actions')
  assert.equal(actions['max-width'], '100%')
  assert.equal(actions.flex, '0 1 auto')
  assert.equal(actions['flex-wrap'], 'wrap')
  assert.equal(declarations('.task-output-actions .button').height, '36px')
})

test('output distribution status is exclusive and independent of asset promotion', async () => {
  const source = readFileSync(new URL('../src/components/TaskMediaInspector.vue', import.meta.url), 'utf8')
  const start = source.indexOf('<span v-if="media.distributionStatus')
  assert.ok(start >= 0)
  // Render the exact production status branches, without mounting a polling task inspector.
  const template = source.slice(start, source.indexOf('</div>', start))
  const render = compile(`<div>${template}</div>`)
  for (const promotedAssetId of ['', 'asset-1']) {
    for (const kind of ['image', 'video']) {
      for (const distributionStatus of ['ready', 'development_master', 'failed', 'not_prepared', 'processing']) {
        const media = {distributionStatus, promotedAssetId, publicLabel:'AI-generated', publicVersion:'v1', distributionError:'checksum mismatch'}
        const html = await renderToString(createSSRApp({render, setup:()=>({media, mediaKind:()=>kind, promotedAssetPurposeLabel:()=> '参考图'})}))
        assert.equal(html.includes('公开副本已验证'), distributionStatus === 'ready')
        assert.equal(html.includes('开发模式 · 可直接下载'), distributionStatus === 'development_master')
        assert.equal(html.includes('公开副本失败：checksum mismatch'), distributionStatus === 'failed')
        assert.equal(html.includes('母版仅供预览，公开下载前需生成带标识副本'), ['not_prepared', 'processing'].includes(distributionStatus))
        assert.equal(html.includes('已保存到素材库'), Boolean(promotedAssetId))
      }
    }
  }
})
async function modules(fn) {
  const vite = await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true}})
  try { await fn(await vite.ssrLoadModule('/src/services/taskDistribution.ts'), vite) } finally { await vite.close() }
}

test('distribution records reject missing, coerced, duplicated and inconsistent scheduling facts', () => modules(async m => {
  const parsed = m.readDistributionPage(page())
  assert.equal(parsed.rows[0].state,'failed'); assert.equal(parsed.count,1)
  for (const changes of [{revision:'1'},{revision:false},{state:'success'},{started_at:'invalid'},
    {finished_at:null},{state:'running'},{kind:'text'},{requested_at:undefined},{error:null}])
    assert.throws(() => m.readDistributionPhase({...phase(),...changes}))
  for (const changes of [{count:'1'},{next:undefined},{results:[phase(),phase()]},{count:0}])
    assert.throws(() => m.readDistributionPage({...page(),...changes}))
  assert.deepEqual(m.distributionRecoveryRequest(parsed.rows[0]),{task_id:taskId,expected_revision:1})
  assert.throws(() => m.distributionRecoveryRequest({...parsed.rows[0],state:'ready'}))
}))

async function component(vite, callback) {
  const {platformApi} = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const previousRead = platformApi.getTaskDistributions, previousWrite = platformApi.retryTaskDistribution
  const {default:Component} = await vite.ssrLoadModule('/src/components/TaskDistributionRecovery.vue')
  const props = reactive({run:{id:'run-1',status:'awaiting_input',planSource:'manual'}})
  let state:any
  const renderer = createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
  const app = renderer.createApp({setup(){state=Component.setup(props,{expose(){},emit(){}});return()=>h('div')}})
  try {app.provide(ssrContextKey,{modules:new Set()});app.mount({});await callback(state,props,platformApi)}
  finally {app.unmount();platformApi.getTaskDistributions=previousRead;platformApi.retryTaskDistribution=previousWrite}
}

test('actual recovery component reads only on demand and hides stale rows after failure or Run change', () => modules(async (_,vite) => {
  await component(vite, async (state,props,api) => {
    let calls=0, complete:any
    api.getTaskDistributions=async()=>{calls++;return page()}
    await nextTick();assert.equal(calls,0)
    await state.load();assert.equal(calls,1);assert.equal(state.detail.value.rows[0].taskId,taskId)
    api.getTaskDistributions=async()=>{throw new Error('read unavailable')}
    await state.load();assert.equal(state.detail.value,null);assert.match(state.error.value,/unavailable/)
    api.getTaskDistributions=()=>new Promise(resolve=>{complete=resolve})
    const old=state.load();props.run={...props.run,id:'run-2'};await nextTick();complete(page());await old
    assert.equal(state.detail.value,null);assert.equal(state.open.value,false)
  })
}))

test('uncertain response reuses exactly the original revision without another generation request', () => modules(async (_,vite) => {
  await component(vite, async (state,_props,api) => {
    const requests:any[]=[]
    state.pending.value={task_id:taskId,expected_revision:1}
    api.retryTaskDistribution=async (_run,request)=>{requests.push({...request});throw new Error('response lost')}
    await state.submit();assert.equal(state.pending.value.expected_revision,1)
    api.retryTaskDistribution=async (_run,request)=>{requests.push({...request});return {...phase(),revision:2,state:'queued',started_at:null,finished_at:null,error:''}}
    api.getTaskDistributions=async()=>page({...phase(),revision:2,state:'queued',started_at:null,finished_at:null,error:''})
    await state.submit()
    assert.deepEqual(requests,[{task_id:taskId,expected_revision:1},{task_id:taskId,expected_revision:1}])
    assert.equal(state.pending.value,null);assert.match(state.notice.value,/未重新调用模型/)
  })
}))

test('same recovery component is reachable for manual and Recipe runs with explicit scope help', () => {
  const view=readFileSync(new URL('../src/views/ProductionRunView.vue',import.meta.url),'utf8')
  assert.equal((view.match(/<TaskDistributionRecovery :run="currentRun"\//g)||[]).length,2)
  const component=readFileSync(new URL('../src/components/TaskDistributionRecovery.vue',import.meta.url),'utf8')
  for(const text of ['仅重新处理副本','不重新调用模型','不直接封存发布包','下载和封存仍会重新核验实际文件','副本处理分页']) assert.ok(component.includes(text),text)
})

test('definitively rejected revision can be reread without getting stuck in uncertain replay', () => modules(async (_,vite) => {
  await component(vite, async (state,_props,api) => {
    state.pending.value={task_id:taskId,expected_revision:1}
    api.retryTaskDistribution=async()=>{throw {isAxiosError:true,message:'revision changed',response:{status:409,data:{detail:'revision changed'}}}}
    await state.submit()
    assert.equal(state.pending.value,null);assert.equal(state.detail.value,null)
    assert.match(state.error.value,/revision/)
    api.getTaskDistributions=async()=>page({...phase(),revision:3})
    await state.load();assert.equal(state.detail.value.rows[0].revision,3)
  })
}))
