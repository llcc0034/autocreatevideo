import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { adaptTaskExecution } from '../src/services/taskExecution.ts'

test('execution adapter accepts explicit null but rejects missing or malformed actual identity', () => {
  assert.equal(adaptTaskExecution(null), null)
  assert.throws(() => adaptTaskExecution(undefined), /缺少/)
  const raw = { production_run_id: 'run', stage_run_id: 'stage', stage_key: 'media_generation', stage_ordinal: 1,
    unit_key: 'opaque-unit', attempt_id: 'attempt', attempt_number: 2, preparation_id: 'prep',
    execution_instance_version_id: 'instance', operation: 'image.generate' }
  assert.equal(adaptTaskExecution(raw)?.attemptNumber, 2)
  assert.throws(() => adaptTaskExecution({ ...raw, attempt_number: 0 }), /无效/)
  assert.throws(() => adaptTaskExecution({ ...raw, attempt_number: true }), /无效/)
  assert.throws(() => adaptTaskExecution({ ...raw, preparation_id: '' }), /缺少/)
})

test('task polling never restores old virtual tasks, and UI counts are scoped to created calls', () => {
  const store = readFileSync(new URL('../src/stores/studio.ts', import.meta.url), 'utf8')
  const view = readFileSync(new URL('../src/components/TaskCenterDetail.vue', import.meta.url), 'utf8')
  for (const old of ['restoreVirtualBatchPlaceholders', 'makeRestoredVirtualBatchUnit', 'stillWaiting', 'batchSlotKey']) assert.ok(!store.includes(old))
  assert.match(view, /仅统计已创建的任务/)
  assert.ok(!view.includes('isVirtualProductionUnit'))
})
