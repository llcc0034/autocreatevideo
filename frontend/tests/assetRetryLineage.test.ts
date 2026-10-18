import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

test('retry lineage shows exact successors and only offers preparation on a failed leaf', async context => {
  const originalStorage = globalThis.localStorage, originalWindow = globalThis.window
  globalThis.localStorage = { getItem: () => null } as any
  let confirmations = 0
  globalThis.window = { confirm: () => { confirmations++; return true } } as any
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  context.after(async () => { await vite.close(); globalThis.localStorage = originalStorage; globalThis.window = originalWindow })
  const { retryFixtureJob, retryInspector } = await vite.ssrLoadModule('/tests/fixtures/asset-retry-lineage.ts')
  const { repository } = await vite.ssrLoadModule('/src/services/repository.ts')
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  api.defaults.adapter = async () => assert.fail('Unexpected business HTTP')
  context.mock.method(globalThis, 'fetch', async () => assert.fail('Unexpected fetch'))
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
  await router.push('/'); await router.isReady()
  const render = async (job: any) => {
    let state: any; const selected: string[] = []
    const app = createSSRApp({ render: () => h(retryInspector(job, value => { state = value }), { onSelect: (id: string) => selected.push(id) }) })
    app.use(createPinia()).use(router)
    return { html: await renderToString(app), state, selected }
  }
  for (const status of ['preparing', 'failed', 'cancelled', 'uncertain', 'succeeded']) {
    const parent = retryFixtureJob('original'); parent.retries[0].status = status
    const view = await render(parent)
    assert.match(view.html, /查看已有重试/); assert.doesNotMatch(view.html, />准备重试<\/button>/)
    await view.state.retryAssetJob(parent)
    assert.equal(confirmations, 0)
  }
  const leaf = retryFixtureJob('successor'), view = await render(leaf)
  assert.match(view.html, /准备重试/); assert.match(view.html, /查看原任务/)
  const calls: string[] = []
  repository.retryAssetGenerationJob = async (id: string) => { calls.push(id); return { id: 'exact-next' } }
  await view.state.retryAssetJob(leaf)
  assert.deepEqual(calls, ['successor']); assert.deepEqual(view.selected, ['exact-next'])
  repository.retryAssetGenerationJob = async () => { throw { response: { status: 409, data: { code: 'execution_preparation_conflict', detail: ['该请求已有重试后继，请读取并处理该任务'] } } } }
  await view.state.retryAssetJob(leaf)
  assert.match(view.state.pollError.value, /已有重试后继/)
  assert.deepEqual(view.selected, ['exact-next'])
})
