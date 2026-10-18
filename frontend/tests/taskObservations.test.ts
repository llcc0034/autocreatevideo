import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { adaptTaskObservations } from '../src/services/taskObservations.ts'

const record = () => ({ id: 'check', task: 'task', kind: 'preflight', status: 'passed', claim_number: 1,
  claim_started_at: '2026-09-10T00:00:00Z', started_at: '2026-09-10T00:00:01Z', finished_at: '2026-09-10T00:00:02Z',
  request_sha256: 'a'.repeat(64), node_count: 3, link_count: 0, schema_sha256: 'b'.repeat(64),
  policy_sha256: '', unload_models: null, free_memory: null, http_status: null, error_code: '' })
const page = (records = [record()]) => ({ count: records.length, next: null, previous: null, results: records })

test('checks retain actual zero, false and missing HTTP rather than substituting success', () => {
  const first = adaptTaskObservations(page(), 'task').results[0]!
  assert.equal(first.linkCount, 0)
  assert.equal(first.httpStatus, null)
  const raw = { ...record(), kind: 'resource_release', status: 'failed', node_count: null, link_count: null,
    schema_sha256: '', policy_sha256: 'c'.repeat(64), unload_models: true, free_memory: false,
    error_code: 'comfyui_resource_release_unconfirmed' }
  const result = adaptTaskObservations({ count: 1, next: null, previous: null, results: [raw] }, 'task').results[0]!
  assert.equal(result.freeMemory, false)
  assert.equal(result.status, 'failed')
  assert.equal(result.httpStatus, null)
})

test('empty checks are empty evidence; missing or contradictory facts are rejected', () => {
  assert.deepEqual(adaptTaskObservations(page([]), 'task').results, [])
  for (const key of ['count', 'next', 'previous', 'results']) {
    const raw: Record<string, unknown> = page()
    delete raw[key]
    assert.throws(() => adaptTaskObservations(raw, 'task'))
  }
  for (const changes of [{ claim_number: true }, { claim_number: 0 }, { task: 'other-task' },
    { status: 'unknown' }, { status: 'failed' }, { node_count: null }, { schema_sha256: '' },
    { finished_at: '2020-01-01T00:00:00Z' }, { request_sha256: '' }]) {
    assert.throws(() => adaptTaskObservations({ ...page(), results: [{ ...record(), ...changes }] }, 'task'))
  }
  assert.throws(() => adaptTaskObservations(page([record(), record()]), 'task'))
})

test('repository requests a single explicit page and propagates errors without fallback', async t => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false } })
  t.after(() => server.close())
  const { api } = await server.ssrLoadModule('/src/services/api.ts')
  const { repository } = await server.ssrLoadModule('/src/services/repository.ts')
  const original = api.get
  t.after(() => { api.get = original })
  const calls: unknown[][] = []
  api.get = async (...args: unknown[]) => { calls.push(args); return { data: page() } }
  const parsed = await repository.getTaskObservations('task', 2)
  assert.equal(parsed.count, 1)
  assert.deepEqual(calls, [['/tasks/task/observations/', { params: { page: 2 } }]])
  const missing = new Error('checks unavailable')
  api.get = async () => { throw missing }
  await assert.rejects(repository.getTaskObservations('task'), missing)
})

test('checks load on demand, guard stale requests and never claim generation success', () => {
  const source = readFileSync(new URL('../src/components/TaskExecutionChecks.vue', import.meta.url), 'utf8')
  assert.match(source, /if \(open.value\) void load\(\)/)
  assert.match(source, /request === sequence && taskId === props.taskId/)
  assert.match(source, /result.value = null/)
  assert.match(source, /检查通过不代表烟测或生成成功/)
  assert.match(source, /尚无检查记录/)
  assert.doesNotMatch(source, /capability_snapshot|setInterval|onMounted/)
})
