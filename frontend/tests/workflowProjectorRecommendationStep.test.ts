import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('Projector recommendation initialization distinguishes modes, actions and scan evidence', async context => {
  const vite = await createServer({server:{middlewareMode:true,hmr:false},appType:'custom',optimizeDeps:{noDiscovery:true}})
  context.after(()=>vite.close())
  const { default: Step } = await vite.ssrLoadModule('/src/components/WorkflowProjectorRecommendationStep.vue')
  const base = {scanState:'idle',scannedCount:0,candidates:[],selectedKey:'',busy:false,recommending:false,unavailableReason:''}
  const render = (props={}) => renderToString(createSSRApp({render:()=>h(Step,{...base,...props})}))
  const ready = {scanState:'fresh',scannedCount:2,candidates:[{key:'candidate-a',label:'来源 · graph.json'}]}
  const button = (html:string,label:string) => [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].map(item=>item[0]).find(item=>item.includes(label)) || ''
  await context.test('unscanned mode explains navigation and has one actionable scan entry',async()=>{
    let emitted=0
    const html=await render({onScan:()=>emitted++,onRecommend:()=>emitted++})
    assert.match(html,/尚未扫描工作流/)
    assert.match(html,/切换初始化方式不会扫描或运行工作流/)
    assert.match(html,/关闭或刷新页面不保留/)
    assert.ok(button(html,'前往扫描工作流'))
    assert.doesNotMatch(button(html,'前往扫描工作流'),/disabled/)
    assert.equal(button(html,'生成推荐配置'),'')
    assert.equal(emitted,0)
  })
  await context.test('loading, failure, partial, empty and unfinished candidates stay distinct',async()=>{
    for (const [props,title] of [
      [{scanState:'loading'},'正在扫描工作流'],
      [{scanState:'failed',...{candidates:ready.candidates}},'扫描未成功'],
      [{scanState:'partial'},'部分来源扫描失败'],
      [{scanState:'fresh'},'扫描完成，未发现工作流'],
      [{scanState:'fresh',scannedCount:5},'候选尚未就绪'],
    ] as const) {
      const html=await render(props)
      assert.ok(html.includes(title),title)
      assert.equal(button(html,'生成推荐配置'),'')
    }
    assert.match(button(await render({scanState:'loading'}),'前往扫描工作流'),/disabled/)
    const partial=await render({...ready,scanState:'partial',selectedKey:'candidate-a'})
    assert.match(partial,/部分来源扫描失败/)
    assert.ok(button(partial,'生成推荐配置'))
    assert.doesNotMatch(button(partial,'生成推荐配置'),/disabled/)
  })
  await context.test('recommendation requires a current selection and blocks busy or unavailable state',async()=>{
    assert.match(button(await render(ready),'生成推荐配置'),/disabled/)
    const selected=await render({...ready,selectedKey:'candidate-a'})
    assert.doesNotMatch(button(selected,'生成推荐配置'),/disabled/)
    const stale=await render({...ready,selectedKey:'old'})
    assert.match(stale,/原候选已不可推荐，请重新选择/)
    assert.match(button(stale,'生成推荐配置'),/disabled/)
    const busy=await render({...ready,selectedKey:'candidate-a',busy:true,recommending:true})
    assert.match(button(busy,'正在生成推荐'),/disabled/)
    assert.match(button(busy,'查看扫描候选'),/disabled/)
    const denied=await render({...ready,selectedKey:'candidate-a',unavailableReason:'需要超级管理员权限'})
    assert.match(denied,/需要超级管理员权限/)
    assert.equal(button(denied,'生成推荐配置'),'')
    assert.match(button(denied,'前往扫描工作流'),/disabled/)
  })
  await context.test('current state replaces old success and candidate labels are escaped',async()=>{
    const html=await render({...ready,candidates:[{key:'candidate-a',label:'<script>alert(1)</script>'}]})
    assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/)
    assert.match(await render({scanState:'failed'}),/旧扫描结果不会用于推荐/)
    assert.equal(button(await render({scanState:'failed',candidates:ready.candidates,selectedKey:'candidate-a'}),'生成推荐配置'),'')
  })
  await context.test('mode navigation, return flow and original scan handler keep one authoritative path',async()=>{
    const settings=await readFile(new URL('../src/views/SettingsView.vue',import.meta.url),'utf8')
    assert.match(settings,/role="radiogroup" aria-label="方案初始化方式"/)
    assert.match(settings,/role="radio" :aria-checked=/)
    assert.match(settings,/navigateWorkflowProjectorCreateMode/)
    assert.match(settings,/ArrowLeft.*ArrowRight.*ArrowUp.*ArrowDown.*Home.*End/)
    const change=settings.slice(settings.indexOf('const changeWorkflowProjectorCreateMode'),settings.indexOf('const initializeBlankWorkflowProjector'))
    assert.match(change,/confirmWorkflowProjectorReinitialize/)
    assert.doesNotMatch(change,/scanSavedWorkflows\(|recommendWorkflowProjectorFromCandidate\(|platformApi\./)
    const go=settings.slice(settings.indexOf('const goToProjectorCandidateScan'),settings.indexOf('const applyWorkflowProjectorCreateSeed'))
    assert.match(go,/if \(!importer\).*return/)
    assert.match(go,/workflowProjectorResumeFromScan.value = true/)
    assert.doesNotMatch(go,/scanSavedWorkflows\(|platformApi\.|Object.assign/)
    assert.ok(go.indexOf('if (!importer)')<go.indexOf('workflowProjectorCreateOpen.value = false'))
    assert.match(settings,/data-projector-scan-action[^\n]+@click="scanSavedWorkflows"/)
    assert.match(settings,/@click="openWorkflowProjectorCreate\(\)">返回新建方案/)
    assert.match(settings,/if \(workflowProjectorResumeFromScan.value && !projector\) \{ workflowProjectorCreateOpen.value = true; return \}/)
    assert.doesNotMatch(settings,/扫描推荐（推荐）|尚无可推荐候选。|前往扫描与纳管（保留表单）/)
  })
})
