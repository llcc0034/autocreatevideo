import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createRouter, createMemoryHistory } from 'vue-router'

test('an open manual stage never claims a completed call is running', async context => {
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel:'silent', appType:'custom', server:{middlewareMode:true,hmr:false} })
  context.after(() => vite.close())
  const { adaptManualExecutionActivity } = await vite.ssrLoadModule('/src/services/productionManual.ts')
  const { default: Component } = await vite.ssrLoadModule('/src/components/ManualExecutionActivity.vue')
  const row = {stage_id:'stage',stage_key:'script_generate',stage_status:'running',lifecycle_label:'步骤开放',preparations:[{status:'dispatched',label:'已派发',count:1}],calls:[{role:'primary',role_label:'主调用',status:'succeeded',label:'成功',count:1}]}
  const [activity] = adaptManualExecutionActivity([row])
  const router = createRouter({history:createMemoryHistory(),routes:[{path:'/workflows',name:'workflows',component:{render:()=>null}}]})
  const render = (props: object) => renderToString(createSSRApp({render:()=>h(Component,{runId:'run',...props})}).use(router))
  const html = await render({activity})
  assert.match(html,/步骤开放/); assert.match(html,/主调用：成功 1 次/)
  assert.doesNotMatch(html,/运行中/); assert.match(html,/查看已有准备与结果/)
  assert.match(html,/href="\/workflows\?category=production&amp;production_run_id=run&amp;stage_run_id=stage"/)
  assert.match(html,/查看本步骤任务/)
  const mixed = await render({activity:{...activity,calls:[...activity.calls,
    {role:'primary',roleLabel:'主调用',status:'failed',label:'失败',count:2},
    {role:'auxiliary',roleLabel:'准备字段调用',status:'running',label:'执行中',count:1}]}})
  for (const label of ['主调用：成功 1 次','主调用：失败 2 次','准备字段调用：执行中 1 次']) assert.ok(mixed.includes(label))
  assert.match(await render({activity:{...activity,calls:[]}}),/尚未发起模型调用/)
  assert.match(await render({}),/调用状态未能读取/)
  assert.match(await render({loading:true}),/正在读取调用状态/)
  for (const broken of [undefined,[{...row,calls:null}],[{...row,calls:[{...row.calls[0],count:0}]}],[row,row]]) {
    assert.throws(()=>adaptManualExecutionActivity(broken))
  }
})
