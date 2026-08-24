import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const projectId = '11111111-1111-4111-8111-111111111111'
const sourceTaskId = '22222222-2222-4222-8222-222222222222'
const targetShotId = '33333333-3333-4333-8333-333333333333'
const requestId = '44444444-4444-4444-8444-444444444444'

const rawDeferred = (status = 'waiting_upstream') => ({
  id: requestId,
  project_id: projectId,
  source_task_id: sourceTaskId,
  source_output_role: 'primary_video',
  target_shot_id: targetShotId,
  frame_slot: 'last_frame',
  target_template_id: '55555555-5555-4555-8555-555555555555',
  target_template_hash: 'a'.repeat(64),
  idempotency_key: 'deferred-key',
  request_fingerprint: 'b'.repeat(64),
  target_input_fingerprint: 'c'.repeat(64),
  status,
  resolved_source_output_index: null,
  source_artifact_sha256: null,
  source_manifest_sha256: null,
  derived_asset_id: null,
  materialized_task_id: null,
  materialized_prompt_artifact_id: null,
  materialized_workflow_id: null,
  priority: 0,
  attempts: 0,
  max_attempts: 3,
  next_attempt_at: null,
  extended_generation_confirmed: false,
  error_code: '',
  error_message: '',
  started_at: null,
  finished_at: null,
  created_at: '2026-08-19T10:00:00Z',
  updated_at: '2026-08-19T10:00:00Z',
  provisional_prompt_text: 'preview prompt',
  provisional_negative_prompt: 'preview negative',
  provisional_prompt_sha256: 'd'.repeat(64),
})

test('deferred generation frontend contract is strict and uses only the public API', async (context) => {
  const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
  const server = await createServer({
    root: frontendRoot,
    logLevel: 'silent',
    appType: 'custom',
    server: { middlewareMode: true, hmr: false },
  })
  context.after(async () => { await server.close() })

  const service = await server.ssrLoadModule('/src/services/deferredGeneration.ts')
  const { repository } = await server.ssrLoadModule('/src/services/repository.ts')
  const { api } = await server.ssrLoadModule('/src/services/api.ts')
  const originalGet = api.get
  context.after(() => { api.get = originalGet })

  await context.test('adapter accepts only the declared status, frame slot and primary video role', () => {
    const adapted = service.adaptDeferredGenerationRequest(rawDeferred())
    assert.equal(adapted.id, requestId)
    assert.equal(adapted.sourceOutputRole, 'primary_video')
    assert.equal(adapted.frameSlot, 'last_frame')
    assert.equal(adapted.status, 'waiting_upstream')
    assert.equal(adapted.provisionalPromptText, 'preview prompt')
    assert.throws(() => service.adaptDeferredGenerationRequest({ ...rawDeferred(), source_output_role: 'first_file' }), /契约无效/)
    assert.throws(() => service.adaptDeferredGenerationRequest({ ...rawDeferred(), status: 'done' }), /契约无效/)
  })

  await context.test('repository exposes diagnostics only', () => {
    assert.equal('createDeferredGenerationRequest' in repository, false)
    assert.equal('cancelDeferredGenerationRequest' in repository, false)
    assert.equal('cancelGenerationTask' in repository, false)
    assert.equal('retryGenerationTask' in repository, false)
  })

  await context.test('list query uses public filters and never reads capability snapshots', async () => {
    let requestedParams: Record<string, unknown> = {}
    api.get = async (url: string, options: { params?: Record<string, unknown> } = {}) => {
      assert.equal(url, '/deferred-generation-requests/')
      requestedParams = options.params || {}
      return { data: { count: 1, next: null, previous: null, results: [rawDeferred()] } }
    }
    const rows = await repository.getDeferredGenerationRequests({ sourceTask: sourceTaskId })
    assert.equal(rows[0].sourceTaskId, sourceTaskId)
    assert.equal(requestedParams.source_task, sourceTaskId)
    assert.equal(requestedParams.page_size, 100)
  })
})

test('WorkflowView keeps deferred continuity read-only without private task snapshots', () => {
  const workflowView = readFileSync(new URL('../src/views/WorkflowView.vue', import.meta.url), 'utf8')
  assert.ok(workflowView.includes('repository.getDeferredGenerationRequests({ sourceTask: taskId })'))
  assert.ok(workflowView.includes('自动连续生成请求'))
  assert.ok(!workflowView.includes('@click="openDeferredGeneration"'))
  assert.ok(!workflowView.includes('@click="createDeferredGeneration"'))
  assert.ok(!workflowView.includes('@click="cancelDeferredGeneration(request)"'))
  assert.ok(!workflowView.includes('capability_snapshot'))
})
