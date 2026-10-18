import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

const settle = async () => { for (let i = 0; i < 20; i++) { await Promise.resolve(); await nextTick() } }
const deferred = () => {
  let resolve!: (value?: any) => void, reject!: (reason: any) => void
  const promise = new Promise<any>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const impact = (id = 'target') => ({
  schemaVersion: 1, requestedAction: 'delete', target: { resourceType: 'model_profile_version', objectId: id, label: id, status: 'draft', stateSha256: 's' },
  targetPolicy: { allowed: true, reasonCode: '', reason: '' }, complete: true, canExecute: true,
  dependencies: [{ dependencyKey: 'draft', disposition: 'selectable_delete' }],
  summary: { selectableCount: 1, automaticCount: 0, blockingCount: 0, preservedHistoryCount: 0 },
  confirmationPhrase: `删除 ${id}`, advisories: [], impactSha256: `hash-${id}`,
})

test('governed deletion review isolates reads, writes and ambiguous outcomes', async t => {
  const previousStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} } as any
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  const { platformApi: api } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.ts')
  const { default: Component } = await vite.ssrLoadModule('/src/components/RegistryDeletionReviewModal.vue')
  const originals = { ...api }
  const mount = async () => {
    const props = reactive({ open: true, resourceType: 'model_profile_version', objectId: 'target' })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
    await router.push('/settings?section=models'); await router.isReady()
    const pinia = createPinia(), auth = useAuthStore(pinia)
    auth.user = { id: 'operator', isStaff: true, isSuperuser: true }
    const events: any[] = []
    let s: any
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    const app = renderer.createApp({ setup() { s = Component.setup(props, { expose() {}, emit: (...args: any[]) => events.push(args) }); return () => h('div') } })
    app.use(pinia); app.use(router); app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await settle()
    return { s, props, router, auth, events, app }
  }
  const review = (s: any) => { s.toggleDependency('draft', true); s.confirmation.value = s.impact.value.confirmationPhrase; s.reasonCode.value = 'planned_change' }
  try {
    await t.test('failed reread retains inputs as stale; successful reread requires a fresh review', async () => {
      api.previewRegistryDeletion = async () => impact()
      const m = await mount(), s = m.s, pending = deferred(); let reads = 0
      try {
        review(s); assert.equal(s.canSubmit.value, true)
        api.previewRegistryDeletion = () => { reads++; return pending.promise }
        const first = s.loadImpact(); await s.loadImpact()
        assert.equal(reads, 1); assert.equal(s.canSubmit.value, false)
        s.toggleDependency('draft', false); assert.equal(s.selectedKeys.value.has('draft'), true)
        pending.reject(Error('offline')); await first
        assert.equal(s.confirmation.value, '删除 target'); assert.equal(s.reasonCode.value, 'planned_change')
        assert.equal(s.impact.value.target.objectId, 'target'); assert.equal(s.stale.value, true)
        api.previewRegistryDeletion = async () => impact(); await s.loadImpact()
        assert.equal(s.stale.value, false); assert.equal(s.confirmation.value, ''); assert.equal(s.selectedKeys.value.size, 0)
        assert.equal(s.canSubmit.value, false)
      } finally { m.app.unmount() }
    })
    for (const change of ['target', 'close', 'account', 'permission', 'route', 'unmount']) {
      await t.test(`late preview cannot cross ${change}`, async () => {
        const pending = deferred(); api.previewRegistryDeletion = () => pending.promise
        const m = await mount()
        api.previewRegistryDeletion = async (_type: string, id: string) => impact(id)
        if (change === 'target') m.props.objectId = 'replacement'
        if (change === 'close') m.props.open = false
        if (change === 'account') m.auth.user.id = 'other'
        if (change === 'permission') m.auth.user.isStaff = false
        if (change === 'route') await m.router.push('/settings?section=workflows')
        if (change === 'unmount') m.app.unmount()
        await settle(); pending.resolve(impact('stale')); await settle()
        assert.notEqual(m.s.impact.value?.target.objectId, 'stale')
        assert.equal(m.events.length, 0)
        if (change !== 'unmount') m.app.unmount()
      })
    }
    await t.test('mismatched preview target is never actionable', async () => {
      api.previewRegistryDeletion = async () => impact('wrong')
      const m = await mount()
      try { assert.equal(m.s.impact.value, null); assert.equal(m.s.canSubmit.value, false); assert.match(m.s.error.value, /目标不一致/) }
      finally { m.app.unmount() }
    })
    await t.test('one immutable submission and one acknowledgement, even without immediate parent close', async () => {
      api.previewRegistryDeletion = async () => impact()
      const m = await mount(), pending = deferred(); let writes = 0, captured: any
      api.executeRegistryDeletion = (...args: any[]) => { writes++; captured = args; return pending.promise }
      try {
        review(m.s); const first = m.s.submit(); await m.s.submit(); await m.s.loadImpact(); m.s.close()
        m.s.toggleDependency('draft', false)
        assert.equal(writes, 1); assert.equal(m.events.length, 0)
        assert.deepEqual(captured, ['model_profile_version', 'target', { expectedImpactSha256: 'hash-target', selectedDependencyKeys: ['draft'], confirmationPhrase: '删除 target', reasonCode: 'planned_change' }])
        pending.resolve(); await first; await m.s.submit()
        assert.equal(writes, 1); assert.equal(m.s.canSubmit.value, false)
        assert.deepEqual(m.events, [['completed', { resourceType: 'model_profile_version', objectId: 'target' }]])
      } finally { m.app.unmount() }
    })
    for (const change of ['target', 'close', 'account', 'permission', 'route', 'unmount']) {
      await t.test(`late deletion acknowledgement cannot cross ${change}`, async () => {
        api.previewRegistryDeletion = async (_type: string, id: string) => impact(id)
        const m = await mount(), pending = deferred()
        api.executeRegistryDeletion = () => pending.promise
        review(m.s); const writing = m.s.submit()
        if (change === 'target') m.props.objectId = 'replacement'
        if (change === 'close') m.props.open = false
        if (change === 'account') m.auth.user.id = 'other'
        if (change === 'permission') m.auth.user.isStaff = false
        if (change === 'route') await m.router.push('/settings?section=workflows')
        if (change === 'unmount') m.app.unmount()
        pending.resolve(); await writing; await settle()
        assert.equal(m.events.length, 0)
        if (change !== 'unmount') m.app.unmount()
      })
    }
    for (const status of [0, 500, 409]) {
      await t.test(`HTTP ${status} preserves review and never claims rollback or retries automatically`, async () => {
        api.previewRegistryDeletion = async () => impact()
        const m = await mount(); let writes = 0
        api.executeRegistryDeletion = async () => { writes++; throw status ? {response:{status,data:{error:{code:'deletion_impact_changed',message:'Rejected'}}}} : Error('network lost') }
        try {
          review(m.s); await m.s.submit(); await m.s.submit()
          assert.equal(writes, 1); assert.equal(m.s.confirmation.value, '删除 target')
          assert.doesNotMatch(m.s.error.value, /回滚/); assert.equal(m.events.length, 0)
          assert.equal(m.s.submissionUnknown.value, status !== 409)
          await m.s.loadImpact(); review(m.s)
          assert.equal(m.s.canSubmit.value, status === 409)
          assert.equal(writes, 1, 'reread is read-only')
        } finally { m.app.unmount() }
      })
    }
  } finally { Object.assign(api, originals); await vite.close(); globalThis.localStorage = previousStorage }
})
