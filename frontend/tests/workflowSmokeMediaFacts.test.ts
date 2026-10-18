import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'

test('workflow smoke separates fixed bindings and staging from the authoritative per-use input', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false, ws: false, watch: null },
    appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { default: Component } = await vite.ssrLoadModule('/src/components/WorkflowSmokeMediaFacts.vue')
  const render = async (changes: Record<string, unknown> = {}) => {
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/', component: {} }, { path: '/workflows', name: 'workflows', component: {} },
    ] })
    await router.push('/')
    await router.isReady()
    return renderToString(createSSRApp({ render: () => h(Component, { attempt: {
      assetBindings: [], result: { evidence: { staged_input_count: 1 } }, preparationId: 'saved-preparation', ...changes,
    } }) }).use(router))
  }
  const dynamic = await render()
  assert.doesNotMatch(dynamic, /本次未绑定素材/)
  assert.match(dynamic, /未记录固定槽位绑定；不代表没有动态参考输入/)
  assert.match(dynamic, /执行暂存回执：1 项/)
  assert.match(dynamic, /不是素材身份、用途或槽位明细/)
  assert.match(dynamic, /id=saved-preparation/)
  assert.match(dynamic, /完整输入、动态参考与调用记录/)
  const fixed = await render({ assetBindings: [
    { semanticKey: 'image.first', inputSlot: 'first', mimeType: 'image/png', checksum: 'a'.repeat(64) },
    { semanticKey: 'image.last', inputSlot: 'last', mimeType: 'image/png', checksum: 'a'.repeat(64) },
  ] })
  assert.match(fixed, /image.first/); assert.match(fixed, /image.last/)
  assert.doesNotMatch(fixed, /未记录固定槽位绑定/)
  for (const count of [undefined, -1, true, '1', 1.5]) {
    const invalid = await render({ result: { evidence: { staged_input_count: count } } })
    assert.match(invalid, /尚无可核对的媒体暂存数量/)
    assert.doesNotMatch(invalid, /执行暂存回执：0 项/)
  }
  assert.match(await render({ result: { evidence: { staged_input_count: 0 } } }), /执行暂存回执：0 项/)
  const missing = await render({ preparationId: null, result: {} })
  assert.match(missing, /缺少准备记录关联/)
  assert.doesNotMatch(missing, /href=/)
  const other = await render({ preparationId: 'other-preparation' })
  assert.match(other, /id=other-preparation/)
  assert.doesNotMatch(other, /saved-preparation/)
  const page = readFileSync('src/views/WorkflowVersionEditorView.vue', 'utf8')
  assert.match(page, /<WorkflowSmokeMediaFacts :attempt="smokeAttempt"\/>/)
  assert.doesNotMatch(page, /本次未绑定素材/)
})
