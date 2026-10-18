import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { retryPreviewFixture } from './fixtures/retry-preview.ts'
import { productionIssuePresentation, productionRunCanRetry } from '../src/services/productionRunPresentation.ts'

test('configured recovery shows real attempt counts and due time and opens only manual recovery', () => {
  const reason = '已尝试 1/3 次；下一次自动重试不早于 2026-10-11T13:00:00+08:00。'
  const waiting = productionIssuePresentation('execution_retry_wait', reason, 'running')
  assert.equal(waiting.summary, reason)
  assert.equal(waiting.isWaiting, true)
  assert.equal(waiting.tone, 'info')
  assert.equal(productionRunCanRetry({status:'running', blockedCode:'execution_retry_wait'}), false)
  for (const code of ['execution_retry_exhausted', 'execution_retry_requires_action']) {
    const item = productionIssuePresentation(code, '已尝试 3/3 次，请人工处理。', 'awaiting_input')
    assert.match(item.summary, /3\/3/)
    assert.equal(item.tone, 'warning')
    assert.equal(productionRunCanRetry({status:'awaiting_input', blockedCode:code}), true)
  }
})

const root = fileURLToPath(new URL('..', import.meta.url))
const server = () => createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
const raw = { id: 'run', plan_source: 'recipe', recipe_version: 'recipe',
  retry_context: { expected_stage_id: 'stage', expected_attempt_ids: ['failed-a'] } }

test('retry context is explicit server data and does not alias mutable polling data', async () => {
  const vite = await server()
  try {
    const { adaptProductionRun } = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const source = structuredClone(raw)
    const run = adaptProductionRun(source)
    source.retry_context.expected_attempt_ids.push('later-failure')
    assert.deepEqual(run.retryContext, { expectedStageId: 'stage', expectedAttemptIds: ['failed-a'] })
    assert.throws(() => adaptProductionRun({ ...raw, retry_context: {} }), /重试目标数据不完整/)
    assert.equal(adaptProductionRun({ ...raw, retry_context: null }).retryContext, null)
  } finally { await vite.close() }
})

test('formal retry API posts the confirmed stage and exact failed attempts', async () => {
  const vite = await server()
  try {
    const { api } = await vite.ssrLoadModule('/src/services/api.ts')
    const calls: unknown[] = []
    api.post = async (...args: unknown[]) => { calls.push(args); return { data: raw } }
    const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
    await platformApi.retryProductionRun('run', { expectedStageId: 'stage', expectedAttemptIds: ['failed-a'] })
    assert.deepEqual(calls, [['/production-runs/run/retry/', {
      expected_stage_id: 'stage', expected_attempt_ids: ['failed-a'],
    }]])
  } finally { await vite.close() }
})

test('assembly retry copies and submits exact original job IDs and input digest without calling models', async () => {
  const vite = await server()
  try {
    const { adaptProductionRun } = await vite.ssrLoadModule('/src/services/platformAdapters.ts')
    const { api } = await vite.ssrLoadModule('/src/services/api.ts')
    const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
    const source = {...raw,retry_context:{expected_stage_id:'stage',expected_attempt_ids:[],expected_assembly_job_ids:['job-a'],expected_input_sha256:'a'.repeat(64)}}
    const context = adaptProductionRun(source).retryContext
    source.retry_context.expected_assembly_job_ids.push('job-b')
    const calls: unknown[] = []
    api.post = async (...args: unknown[]) => { calls.push(args); return {data:raw} }
    await platformApi.retryProductionRun('run',context)
    assert.deepEqual(calls,[['/production-runs/run/retry/',{expected_stage_id:'stage',expected_attempt_ids:[],expected_assembly_job_ids:['job-a'],expected_input_sha256:'a'.repeat(64)}]])
    for (const value of [undefined,'invalid']) assert.throws(()=>adaptProductionRun({...source,retry_context:{...source.retry_context,expected_input_sha256:value}}))
  } finally { await vite.close() }
})

test('formal retry preview preserves the page envelope and confirmed scope', async () => {
  const vite = await server()
  try {
    const { api } = await vite.ssrLoadModule('/src/services/api.ts')
    const calls: unknown[] = []
    const payload = { ...retryPreviewFixture(), results: [retryPreviewFixture().results[0]],
      next: '/api/v1/production-runs/run/retry-preview/?page=2', previous: null }
    api.get = async (...args: unknown[]) => { calls.push(args); return { data: payload } }
    const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
    const result = await platformApi.getProductionRetryPreview('run', 'stage', 1)
    assert.deepEqual(calls, [['/production-runs/run/retry-preview/', {
      params: { expected_stage_id: 'stage', page: 1, page_size: 20 },
    }]])
    assert.deepEqual(result.retryContext, { expectedStageId: 'stage', expectedAttemptIds: ['failed'] })
    assert.equal(result.count, payload.count)
    assert.equal(result.call_count, payload.call_count)
    assert.equal(result.preparation_count, payload.preparation_count)
    assert.equal(result.can_submit, true)
    assert.equal(result.next, payload.next)
    assert.equal(result.results.length, 1)
    assert.equal(result.results[0].attempt_id, 'failed')
  } finally { await vite.close() }
})

test('existing confirmation retains exact retry targets and explains frozen parameters', () => {
  const view = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
  assert.ok(view.includes('expectedAttemptIds: [...run.retryContext.expectedAttemptIds]'))
  assert.ok(view.includes('JSON.stringify(context) !== JSON.stringify(run.retryContext)'))
  assert.ok(view.includes('种子不变'))
  assert.ok(view.includes('已成功候选不重跑'))
  assert.ok(view.includes('新候选在合法范围内抽取并冻结'))
  assert.ok(view.includes('失败重试、刷新和重复提交不重抽'))
  assert.ok(view.includes('固定种子或合法值用尽会阻止追加'))
  assert.ok(view.includes("['awaiting_approval','blocked'].includes(currentRun.status)&&currentStage&&costGate"))
  assert.ok(view.includes("execution_external_authorization_required"))
  assert.ok(view.includes('productionRunCanRetry(currentRun)'))
  assert.ok(!view.includes("execution_budget_exceeded"))
})

test('material recovery exposes original retry preview without treating ordinary input waits as retries', async () => {
  const vite = await server()
  try {
    const { productionRunCanRetry, productionIssuePresentation } = await vite.ssrLoadModule('/src/services/productionRunPresentation.ts')
    assert.equal(productionRunCanRetry({ status: 'awaiting_input', blockedCode: 'waiting_material_recovery' }), true)
    for (const status of ['running', 'succeeded', 'cancelled']) {
      assert.equal(productionRunCanRetry({ status, blockedCode: 'waiting_material_recovery' }), false)
    }
    assert.equal(productionRunCanRetry({ status: 'awaiting_input', blockedCode: 'waiting_material_source' }), false)
    assert.equal(productionRunCanRetry({ status: 'blocked', blockedCode: 'execution_candidates_incomplete' }), true)
    assert.equal(productionRunCanRetry({ status: 'awaiting_approval', blockedCode: 'execution_resource_confirmation_required' }), true)
    const issue = productionIssuePresentation('waiting_material_recovery', '', 'awaiting_input')
    assert.equal(issue.tone, 'warning')
    assert.equal(issue.isWaiting, false)
    assert.match(issue.summary, /确认前不会新增调用/)
    const view = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')
    assert.ok(view.includes('productionRunCanRetry(currentRun)&&!isUncertainRemote'))
    assert.ok(view.includes('ProductionRetryPreview'))
  } finally { await vite.close() }
})
