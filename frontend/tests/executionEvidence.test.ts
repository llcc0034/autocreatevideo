import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'

const root = fileURLToPath(new URL('..', import.meta.url))
const response = {
  count: 1, next: null, previous: null,
  summary: { identity: { version_id: 'version', definition_sha256: 'definition-sha', target_fingerprint: 'target-sha',
    operation: 'prompt.draft', mode: '', scope: 'instance_smoke' }, read_at: '2026-09-08T01:00:00Z', evidence_state: 'expired',
    latest_completed_attempt_id: 'attempt', finished_at: '2026-09-07T01:00:00Z', expires_at: '2026-09-07T02:00:00Z',
    deployment_id: 'deployment', workflow_version_id: null, prompt_mode: 'manual', prompt_profile_version_id: null,
    policies: { schema_version: 1, values: { 'execution.admission.capacity_policy': 'require_evidence' }, sources: {}, revision_ids: ['config'] } },
  results: [{ attempt_id: 'attempt', attempt_kind: 'deployment_smoke', status: 'succeeded', created_at: '2026-09-07T00:00:00Z',
    updated_at: '2026-09-07T01:00:00Z', submission_started_at: '2026-09-07T00:59:00Z', finished_at: '2026-09-07T01:00:00Z',
    evidence_expires_at: '2026-09-07T02:00:00Z', target_sha256: 'target-sha', error_code: '', estimated_cost: '0.1', actual_cost: null,
    cost_currency: 'USD', preparation_id: 'prep', preparation_status: 'dispatched', preparation_revision: 4,
    preparation_reason_code: '', request_sha256: 'request-sha' }],
}

test('task detail polls active reads, pauses edits/hidden pages, and stops on completion, error or unmount', async t => {
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  const previousDocument=globalThis.document, previousStorage=globalThis.localStorage
  let app:any
  try {
    globalThis.localStorage={getItem:()=>null} as any
    globalThis.document={hidden:false} as any
    const {createPinia}=await import('pinia')
    const {taskCenterApi:api}=await vite.ssrLoadModule('/src/services/taskCenterApi.ts')
    const {default:Panel}=await vite.ssrLoadModule('/src/components/TaskCenterDetail.vue')
    let value:any={id:'prep',kind:'preparation',status:'running',deferred_fields:[]}, reads=0,fail=false,state:any
    api.detail=async()=>{reads++;if(fail)throw Error('断线');return structuredClone(value)}
    t.mock.timers.enable({apis:['setTimeout']})
    const renderer=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){}})
    app=renderer.createApp({setup(){state=Panel.setup(reactive({kind:'preparation',id:'prep'}),{emit(){},expose(){}});return()=>h('div')}})
    app.use(createPinia());app.provide(ssrContextKey,{modules:new Set()});app.mount({})
    await nextTick();assert.equal(reads,1)
    t.mock.timers.tick(5000);await nextTick();assert.equal(reads,2)
    state.dirty.value=true;t.mock.timers.tick(5000);await nextTick();assert.equal(reads,2)
    state.dirty.value=false;(globalThis.document as any).hidden=true
    t.mock.timers.tick(5000);await nextTick();assert.equal(reads,2)
    ;(globalThis.document as any).hidden=false
    value.status='succeeded';t.mock.timers.tick(5000);await nextTick();assert.equal(reads,3)
    t.mock.timers.tick(20000);await nextTick();assert.equal(reads,3)
    state.busy.value=true;state.changed();await nextTick();assert.equal(reads,3)
    state.busy.value=false;await nextTick();await nextTick();assert.equal(reads,4)
    fail=true;await state.pageCalls(1);assert.equal(state.callPage.value,1)
    const failedReads=reads;t.mock.timers.tick(20000);await nextTick();assert.equal(reads,failedReads)
    fail=false;value.status='queued';await state.load()
    const beforeUnmount=reads;app.unmount();app=null
    t.mock.timers.tick(20000);await nextTick();assert.equal(reads,beforeUnmount)
  }finally{app?.unmount();t.mock.timers.reset();globalThis.document=previousDocument;globalThis.localStorage=previousStorage;await vite.close()}
})

test('smoke cancellation follows original task submission boundary', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const { canRequestSmokeCancellation: canCancel } = await vite.ssrLoadModule('/src/services/executionEvidence.ts')
    for (const attemptKind of ['deployment_smoke', 'workflow_smoke']) {
      for (const status of ['preparing', 'queued']) assert.equal(canCancel({ attemptKind, status, submissionStartedAt: null }), true)
      for (const status of ['succeeded', 'failed', 'cancelled', 'uncertain', 'cancel_requested']) {
        assert.equal(canCancel({ attemptKind, status, submissionStartedAt: null }), false)
        assert.equal(canCancel({ attemptKind, status, submissionStartedAt: '2026-09-14T00:00:00Z' }), false)
      }
      for (const status of ['submitting', 'submitted', 'running']) {
        assert.equal(canCancel({ attemptKind, status, submissionStartedAt: '2026-09-14T00:00:00Z' }), attemptKind === 'workflow_smoke')
      }
    }
  } finally { await vite.close() }
})

test('evidence preserves exact scope, frozen expiry and unknown actual cost without claiming admission', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const { adaptExecutionEvidence: adapt } = await vite.ssrLoadModule('/src/services/executionEvidence.ts')
    const page = adapt(response)
    assert.equal(page.summary.evidenceState, 'expired')
    assert.equal(page.results[0].status, 'succeeded')
    assert.equal(page.results[0].actualCost, null)
    assert.equal(page.results[0].preparationId, 'prep')
    assert.equal('canSubmit' in page.summary, false)
    const missing = structuredClone(response)
    missing.summary.evidence_state = 'missing'
    missing.summary.latest_completed_attempt_id = null
    missing.summary.finished_at = null
    missing.summary.expires_at = null
    assert.equal(adapt(missing).summary.evidenceState, 'missing')
    for (const mutate of [
      r => { delete r.summary }, r => { delete r.results[0].actual_cost },
      r => { r.summary.identity.scope = 'resource' }, r => { r.results[0].attempt_kind = 'workflow_smoke' },
      r => { r.summary.expires_at = null }, r => { r.summary.prompt_mode = 'profile' },
      r => { r.summary.latest_completed_attempt_id = null }, r => { r.results[0].preparation_revision = '4' },
    ]) {
      const bad = structuredClone(response)
      mutate(bad)
      assert.throws(() => adapt(bad), /响应不完整/)
    }
  } finally { await vite.close() }
})

test('evidence HTTP query keeps pagination and summary, and only calls the formal read endpoint', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
  try {
    const { api } = await vite.ssrLoadModule('/src/services/api.ts')
    const calls = []
    api.get = async (url, options) => { calls.push({ url, options }); return { data: response } }
    api.post = () => { throw new Error('Reading evidence must not submit') }
    const { executionRegistryApi } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
    const result = await executionRegistryApi.evidence('version/a', 2, 'succeeded')
    assert.equal(result.summary.evidenceState, 'expired')
    assert.equal(result.count, 1)
    assert.deepEqual(calls, [{ url: '/model-execution-instance-versions/version%2Fa/evidence/', options: { params: { page: 2, status: 'succeeded' } } }])
  } finally { await vite.close() }
})
