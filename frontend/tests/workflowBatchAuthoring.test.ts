import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, ssrContextKey } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { installReads } from './fixtures/workflow-import-data.ts'
import { batchScan, batchPreview, batchReceipt } from './fixtures/workflow-batch-data.ts'
const settle = async () => { for (let i = 0; i < 40; i++) { await Promise.resolve(); await nextTick() } }

test('multi-source batch authoring', async t => {
  const storage = globalThis.localStorage, win = globalThis.window, nav = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  const values = new Map<string, string>()
  globalThis.localStorage = { getItem: (k: string) => values.get(k) || null, setItem: (k: string, v: string) => values.set(k, v), removeItem: (k: string) => values.delete(k) } as any
  globalThis.window = { localStorage: globalThis.localStorage, setInterval: () => 1, clearInterval() {}, setTimeout: () => 1, clearTimeout() {} } as any
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { locks: { request: async (_key: string, work: any) => work() } } })
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  const { platformApi: api } = await vite.ssrLoadModule('/src/services/platformApi.ts'), originals = { ...api }
  const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.ts')
  const { default: Component } = await vite.ssrLoadModule('/src/views/SettingsView.vue')
  async function mount() {
    values.clear(); installReads(api)
    api.scanComfySavedWorkflows = async () => batchScan()
    api.previewComfySavedWorkflow = async (id: string, input: any) => batchPreview(id, input)
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { render: () => null } }] })
    await router.push('/settings?section=workflows'); await router.isReady()
    const pinia = createPinia(); useAuthStore(pinia).user = { id: 'batch-test', isStaff: true, isSuperuser: true }
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    let s: any
    const app = renderer.createApp({ setup() { s = Component.setup({}, { expose() {} }); return () => h('div') } })
    app.use(pinia); app.use(router); app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await settle(); await s.scanSavedWorkflows()
    for (const row of s.savedWorkflowItems.value) { await s.previewSavedWorkflow(row); s.savedWorkflowDrafts[s.savedWorkflowKey(row)].selected = true }
    assert.equal(s.readySavedWorkflowItems.value.length, 3)
    return { s, app }
  }
  try {
    await t.test('confirmed receipt presentation names sources and excludes unresolved batches', async () => {
      const view = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
      assert.match(view, /已确认回执 ·/)
      assert.match(view, /来源：.*row\.sourceDeploymentId.*sourceLabel/)
      assert.match(view, /未提交或结果未知的批次不计入/)
      assert.match(view, /saved-workflow-results \.button\{min-height:44px\}/)
    })
    await t.test('item authority is validated before a request can be settled', async () => {
      const { assertSavedWorkflowBatchResultMatchesRequest: validate } = await vite.ssrLoadModule('/src/services/savedWorkflowBatch.ts')
      const input = { sourcePath: 'workflows/shared.json', expectedSha256: '1'.repeat(64), importKind: 'template_version' }
      const result = batchReceipt('batch-a', [input]); result.items[0].sourceDeploymentId = 'batch-b'
      assert.throws(() => validate('batch-a', [input], result), /来源 deployment/)
    })
    await t.test('source groups and limits preserve exact contracts despite identical paths', async () => {
      const { s, app } = await mount(); const calls: any[] = []
      try {
        api.batchImportComfySavedWorkflows = async (id: string, items: any[]) => { calls.push({ id, items }); return batchReceipt(id, items) }
        await s.batchImportSavedWorkflows()
        assert.deepEqual(calls.map(row => [row.id, row.items[0].contractDraft]), [['batch-a', 'contract-a1'], ['batch-a', 'contract-a2'], ['batch-b', 'contract-b1']])
        assert.ok(calls.every(row => row.items.length === 1))
        assert.deepEqual(s.savedWorkflowItems.value.map((row: any) => row.workflowVersionId), ['version-a1', 'version-b1', 'version-a2'])
        assert.equal(s.savedWorkflowResult.value.createdCount, 3); assert.equal(s.savedWorkflowPending.value.length, 0)
        assert.match(s.savedWorkflowImportProgress.value, /按 3 批处理/)
      } finally { app.unmount() }
    })
    await t.test('pending second batch is visible and neither double dispatch nor third batch occurs early', async () => {
      const { s, app } = await mount(); const calls: any[] = []; let release!: (value: any) => void
      try {
        api.batchImportComfySavedWorkflows = async (id: string, items: any[]) => { calls.push({ id, items }); if (calls.length === 2) return new Promise(resolve => { release = resolve }); return batchReceipt(id, items) }
        const saving = s.batchImportSavedWorkflows()
        for (let i = 0; i < 10 && calls.length < 2; i++) { await settle(); await new Promise(r => setImmediate(r)) }
        assert.equal(calls.length, 2); assert.match(s.savedWorkflowImportProgress.value, /第 2\/3 批.*已确认 1 批/)
        await s.batchImportSavedWorkflows(); assert.equal(calls.length, 2)
        const mismatched = batchReceipt(calls[1].id, calls[1].items); mismatched.items[0].sourceDeploymentId = 'batch-b'
        release(mismatched); await saving
        assert.equal(calls.length, 2); assert.equal(s.savedWorkflowResult.value.createdCount, 1)
        assert.equal(s.savedWorkflowPending.value.length, 1)
        assert.equal(s.savedWorkflowPending.value[0].deploymentId, 'batch-a')
        assert.match(s.savedWorkflowImportProgress.value, /第 2 批结果待核对.*其后的 1 批未提交/)
        assert.equal(s.savedWorkflowItems.value.find((row: any) => row.sourceKey === 'b1').imported, false)
      } finally { app.unmount() }
    })
    await t.test('explicit item failure preserves its draft and permits the other source to complete', async () => {
      const { s, app } = await mount(); let calls = 0
      try {
        api.batchImportComfySavedWorkflows = async (id: string, items: any[]) => { calls++; return batchReceipt(id, items, 'item-failure') }
        await s.batchImportSavedWorkflows()
        assert.equal(calls, 3); assert.equal(s.savedWorkflowResult.value.status, 'partial')
        assert.equal(s.savedWorkflowResult.value.createdCount, 2); assert.equal(s.savedWorkflowResult.value.failedCount, 1)
        const row = s.savedWorkflowItems.value.find((row: any) => row.sourceKey === 'a2')
        assert.equal(row.imported, false); assert.equal(s.savedWorkflowDrafts[s.savedWorkflowKey(row)].preview.contractDraftId, 'contract-a2')
        assert.equal(s.savedWorkflowPending.value.length, 0)
      } finally { app.unmount() }
    })
  } finally { Object.assign(api, originals); await vite.close(); globalThis.localStorage = storage; globalThis.window = win; if (nav) Object.defineProperty(globalThis, 'navigator', nav) }
})
