import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { adaptTaskWorkflowExport } from '../src/services/taskWorkflowExport.ts'

const response = () => ({
  task_id: 'task', attempt_id: 'attempt', preparation_id: 'preparation', execution_instance_version_id: 'instance-version',
  workflow_version_id: 'workflow-version', request_sha256: 'request-sha', definition_sha256: 'definition-sha',
  graph_state: 'staged', missing_receipt_count: 0, workflow: { '1': { class_type: 'SaveImage', inputs: {} } },
  content_hash: 'graph-sha', filename: 'graph.json', format: 'comfyui_api', import_ready: true,
  comfyui_url: '', safe_to_open: false, direct_open_supported: false, direct_open_url: null,
  open_mode: 'download_import', download_url: '/tasks/task/comfyui-workflow/?download=true',
  api_download_url: '/tasks/task/comfyui-workflow/?download=true', ui_download_url: '/tasks/task/comfyui-workflow/?workflow_format=ui&download=true',
  available_formats: ['comfyui_api', 'comfyui_ui'], contains_staged_placeholders: false, staged_placeholder_count: 0,
  open_reason: 'Frozen request, no provider call.',
})

test('export exposes actual call/preparation/version and distinct request vs file hashes', () => {
  const parsed = adaptTaskWorkflowExport(response())
  assert.equal(parsed.preparationId, 'preparation')
  assert.equal(parsed.attemptId, 'attempt')
  assert.equal(parsed.workflowVersionId, 'workflow-version')
  assert.equal(parsed.requestSha256, 'request-sha')
  assert.equal(parsed.contentHash, 'graph-sha')
  assert.equal(parsed.importReady, true)
  assert.equal('generatedWorkflowId' in parsed, false)
  assert.equal('injectedValues' in parsed, false)
})

test('missing receipts remain visible and never default to ready', () => {
  const parsed = adaptTaskWorkflowExport({ ...response(), graph_state: 'prepared', missing_receipt_count: 2,
    contains_staged_placeholders: true, staged_placeholder_count: 3, import_ready: false })
  assert.equal(parsed.importReady, false)
  assert.equal(parsed.missingReceiptCount, 2)
  for (const key of ['preparation_id', 'attempt_id', 'workflow_version_id', 'import_ready', 'graph_state', 'staged_placeholder_count']) {
    const raw: Record<string, unknown> = response()
    delete raw[key]
    assert.throws(() => adaptTaskWorkflowExport(raw))
  }
})

test('legacy graph shape and malformed counter are not interpreted as current evidence', () => {
  assert.throws(() => adaptTaskWorkflowExport({ generated_workflow_id: 'old', workflow_json: {} }))
  assert.throws(() => adaptTaskWorkflowExport({ ...response(), missing_receipt_count: true }))
  assert.throws(() => adaptTaskWorkflowExport({ ...response(), workflow: [] }))
})

test('repository uses only the task endpoint, propagates errors and needs no old trace id', async (t) => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false } })
  t.after(() => server.close())
  const { api } = await server.ssrLoadModule('/src/services/api.ts')
  const { repository } = await server.ssrLoadModule('/src/services/repository.ts')
  const original = api.get
  t.after(() => { api.get = original })
  const calls: unknown[][] = []
  api.get = async (...args: unknown[]) => { calls.push(args); return { data: response() } }
  const parsed = await repository.getTaskWorkflowExport({ id: 'task' })
  assert.equal(parsed.preparationId, 'preparation')
  assert.deepEqual(calls, [['/tasks/task/comfyui-workflow/', { params: { workflow_format: 'api' } }]])
  const missing = Object.assign(new Error('actual preparation unavailable'), { response: { status: 404 } })
  calls.length = 0
  api.get = async (...args: unknown[]) => { calls.push(args); throw missing }
  await assert.rejects(repository.getTaskWorkflowExport({ id: 'task', generatedWorkflowId: 'old-trace' }), missing)
  assert.equal(calls.length, 1)
})
