import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const sha = 'a'.repeat(64)
const fixture = () => ({
  run: { id: 'run', planSource: 'manual', status: 'running', executionPlan: { sha256: sha },
    stages: [{ id: 'stage', stageKey: 'media_generation', status: 'running' }] },
  preparation: { id: 'preparation', stageRun: 'stage', unitKey: 'opaque-unit', purpose: 'production', revision: 4,
    status: 'dispatched', requestFrozenAt: '2026-09-29', intent: { request: { requested: {
      stage_key: 'media_generation', run_plan_sha256: sha, scope: { kind: 'shot', id: 'shot-id' },
    } } }, presentation: { kind: 'primary', task: { id: 'failed-call', status: 'failed', errorCode: 'execution_workflow_failed' } } },
  options: { runId: 'run', runPlanSha256: sha, actions: [{ action: 'retry', stageKey: 'media_generation', scopeKinds: ['shot'] }] },
})
const receipt = { id: 'receipt', action: 'retry', result_kind: 'preparation', target_id: 'preparation' }
const settle = async () => { for (let i = 0; i < 25; i++) { await Promise.resolve(); await nextTick() } }

test('manual primary retry uses original identities, receipt and request across uncertain responses', async t => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const m = await vite.ssrLoadModule('/src/services/manualPrimaryRetry.ts')
    const { platformApi: api } = await vite.ssrLoadModule('/src/services/platformApi.ts')
    const { default: C } = await vite.ssrLoadModule('/src/components/ManualPrimaryRetry.vue')
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
      insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    const mount = async (f = fixture()) => {
      api.getManualProductionActions = async () => f.options
      const props = reactive({ run: f.run, preparation: f.preparation, disabled: false }), events: any[] = []
      let state: any
      const app = renderer.createApp({ setup() { state = C.setup(props, { expose() {}, emit: (...args) => events.push(args) }); return () => h('div') } })
      app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await settle()
      return { state, props, events, app }
    }
    await t.test('build copies explicit scope, does not parse opaque unit or include generation inputs', () => {
      const f = fixture(), p = m.buildManualPrimaryRetry(f.run, f.preparation, f.options, 'key')
      assert.deepEqual(p.scope, { kind: 'shot', id: 'shot-id' }); assert.equal(p.unit_key, 'opaque-unit')
      assert.deepEqual(p.payload, { preparation_id: 'preparation', expected_preparation_revision: 4, expected_attempt_id: 'failed-call' })
      assert.equal(p.expected_selection_revision, null)
      f.preparation.intent.request.requested.scope.id = 'later'
      assert.equal(p.scope.id, 'shot-id')
      for (const status of ['queued', 'running', 'succeeded', 'cancelled', 'awaiting_review', 'unknown']) {
        const bad = fixture(); bad.preparation.presentation.task.status = status
        assert.throws(() => m.buildManualPrimaryRetry(bad.run, bad.preparation, bad.options, 'key'))
      }
      for (const change of [f => f.options.actions = [], f => f.run.status = 'cancelled', f => f.run.planSource = 'recipe',
        f => f.options.runId = 'different', f => f.options.runPlanSha256 = 'b'.repeat(64),
        f => f.preparation.intent.request.requested.scope = null, f => f.preparation.presentation.kind = 'auxiliary',
        f => f.preparation.stageRun = 'other', f => f.run.stages[0].status = 'succeeded']) {
        const bad = fixture(); change(bad)
        assert.throws(() => m.buildManualPrimaryRetry(bad.run, bad.preparation, bad.options, 'key'))
      }
      m.verifyManualPrimaryRetryReceipt(receipt, p)
      for (const bad of [{ ...receipt, action: 'generate' }, { ...receipt, target_id: 'other' }, { ...receipt, result_kind: 'artifact' }]) {
        assert.throws(() => m.verifyManualPrimaryRetryReceipt(bad, p))
      }
    })
    await t.test('requires confirmation, prevents double click, receipt means evaluated not success', async () => {
      let finish!: (v: any) => void, calls = 0
      api.performManualProductionAction = async () => { calls++; return new Promise(resolve => { finish = resolve }) }
      const { state: s, events, app } = await mount()
      try {
        await s.submit(); assert.equal(calls, 0)
        s.acknowledged.value = true; const one = s.submit(); await s.submit(); assert.equal(calls, 1)
        finish(receipt); await one; await settle()
        assert.equal(s.pending.value, null); assert.match(s.notice.value, /不代表调用已提交或成功/)
        assert.equal(events.filter(([event]) => event === 'changed').length, 1)
      } finally { app.unmount() }
    })
    await t.test('unknown remote result and unreconciled cost never offer a new primary retry', async () => {
      for (const code of ['uncertain_remote_response', 'actual_cost_reconciliation_required']) {
        const f = fixture(); f.preparation.presentation.task.errorCode = code
        assert.throws(() => m.buildManualPrimaryRetry(f.run, f.preparation, f.options, 'key'), /核对|不确定/)
        let calls = 0
        api.performManualProductionAction = async () => { calls++; return receipt }
        const { state: s, app } = await mount(f)
        try {
          assert.match(s.contextError.value, /核对|不确定/)
          s.acknowledged.value = true; await s.submit()
          assert.equal(calls, 0); assert.equal(s.pending.value, null)
        } finally { app.unmount() }
      }
    })
    await t.test('same failed identity becoming uncertain revokes confirmation without erasing a pending command', async () => {
      const requests: any[] = []
      api.performManualProductionAction = async (_id, request) => {
        requests.push(JSON.parse(JSON.stringify(request))); throw new Error('receipt lost')
      }
      const { state: s, props, app } = await mount()
      try {
        s.acknowledged.value = true
        props.preparation.presentation.task.errorCode = 'uncertain_remote_response'; await settle()
        assert.equal(s.acknowledged.value, false); assert.match(s.contextError.value, /核对|不确定/)
        await s.submit(); assert.equal(requests.length, 0)
        props.preparation.presentation.task.errorCode = 'execution_workflow_failed'; await settle()
        assert.equal(s.contextError.value, ''); assert.equal(s.acknowledged.value, false)
        s.acknowledged.value = true; await s.submit()
        props.preparation.presentation.task.errorCode = 'uncertain_remote_response'; await settle()
        assert.ok(s.pending.value)
        api.performManualProductionAction = async (_id, request) => {
          requests.push(JSON.parse(JSON.stringify(request))); return receipt
        }
        await s.submit(); assert.deepEqual(requests[1], requests[0]); assert.equal(s.pending.value, null)
      } finally { app.unmount() }
    })
    await t.test('unknown result retains same request even if source props change; replay rejection keeps it', async () => {
      const requests: any[] = []
      api.performManualProductionAction = async (_id, request) => { requests.push(JSON.parse(JSON.stringify(request))); throw new Error('network lost') }
      const { state: s, props, app } = await mount()
      try {
        s.acknowledged.value = true; await s.submit(); assert.ok(s.pending.value)
        props.preparation.revision = 19; props.preparation.presentation.task.id = 'new-call'; await settle()
        api.performManualProductionAction = async (_id, request) => { requests.push(JSON.parse(JSON.stringify(request))); throw { response: { status: 400 } } }
        await s.submit(); assert.ok(s.pending.value); assert.deepEqual(requests[0], requests[1])
        api.performManualProductionAction = async (_id, request) => { requests.push(JSON.parse(JSON.stringify(request))); return receipt }
        await s.submit(); assert.deepEqual(requests[0], requests[2]); assert.equal(s.pending.value, null)
      } finally { app.unmount() }
    })
    await t.test('parent polling replaces same-identity objects without losing an in-flight or uncertain command', async () => {
      const requests: any[] = []
      let reject!: (reason: Error) => void
      api.performManualProductionAction = async (_id, request) => {
        requests.push(JSON.parse(JSON.stringify(request)))
        return new Promise((_resolve, no) => { reject = no })
      }
      const { state: s, props, events, app } = await mount()
      try {
        s.acknowledged.value = true
        const first = s.submit()
        props.run = JSON.parse(JSON.stringify(props.run))
        props.preparation = JSON.parse(JSON.stringify(props.preparation))
        await settle()
        assert.equal(s.submitting.value, true)
        assert.deepEqual(JSON.parse(JSON.stringify(s.pending.value)), requests[0])
        reject(new Error('accepted command receipt was lost')); await first; await settle()
        assert.equal(s.submitting.value, false); assert.match(s.error.value, /receipt was lost/)
        props.run = JSON.parse(JSON.stringify(props.run)); await settle()
        assert.deepEqual(JSON.parse(JSON.stringify(s.pending.value)), requests[0])
        assert.equal(events.filter(([event, value]) => event === 'dirty' && value === false).length, 0)
        api.performManualProductionAction = async (_id, request) => {
          requests.push(JSON.parse(JSON.stringify(request))); return receipt
        }
        await s.submit(); await settle()
        assert.deepEqual(requests[1], requests[0]); assert.equal(s.pending.value, null)
        assert.equal(events.filter(([event]) => event === 'changed').length, 1)
      } finally { app.unmount() }
    })
    await t.test('definitive first rejection reloads context, mismatch receipt retains request', async () => {
      api.performManualProductionAction = async () => { throw { response: { status: 400 } } }
      const { state: s, app } = await mount()
      try {
        s.acknowledged.value = true; await s.submit(); assert.equal(s.pending.value, null); assert.equal(s.options.value, null)
        await s.load(); s.acknowledged.value = true
        api.performManualProductionAction = async () => ({ ...receipt, target_id: 'wrong' })
        await s.submit(); assert.ok(s.pending.value); assert.match(s.error.value, /原准备/)
      } finally { app.unmount() }
    })
    await t.test('late command response cannot update a different preparation', async () => {
      let finish!: (v: any) => void
      api.performManualProductionAction = async () => new Promise(resolve => { finish = resolve })
      const { state: s, props, events, app } = await mount()
      try {
        s.acknowledged.value = true; const submitting = s.submit()
        props.preparation = { ...props.preparation, id: 'different-preparation' }; await settle()
        finish(receipt); await submitting
        assert.equal(s.notice.value, ''); assert.equal(events.filter(([event]) => event === 'changed').length, 0)
      } finally { app.unmount() }
    })
    await t.test('same running call becoming failed loads retry options without changing attempt identity', async () => {
      const f = fixture(); f.preparation.presentation.task.status = 'running'
      const { state: s, props, app } = await mount(f)
      try {
        assert.equal(s.options.value, null)
        props.preparation.presentation.task.status = 'failed'; await settle()
        assert.equal(s.options.value.runId, 'run'); assert.equal(s.contextError.value, '')
        assert.equal(props.preparation.presentation.task.id, 'failed-call')
        s.acknowledged.value = true
        props.preparation.presentation.task.status = 'succeeded'; await settle()
        assert.equal(s.acknowledged.value, false); assert.equal(s.failed.value, false)
      } finally { app.unmount() }
    })
  } finally { await vite.close() }
})
