import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildTaskListEntries,
  taskListFilterCounts,
  taskListGroupKey,
} from '../src/services/taskGroups.ts'
import type { Workflow } from '../src/types/index.ts'

const workflow = (id: string, values: Partial<Workflow> = {}): Workflow => ({
  id,
  taskId: id,
  projectId: 'project-1',
  projectTitle: `Project · ${id}`,
  status: 'queued',
  progress: 0,
  currentStep: 'waiting',
  nodes: [],
  createdAt: '2026-08-24T01:00:00Z',
  episodeId: 'episode-1',
  episodeNumber: 1,
  episodeTitle: 'Pilot',
  shotId: `shot-${id}`,
  shotNumber: Number(id.replace(/\D/g, '')) || 1,
  ...values,
})

test('ProductionRun tasks merge only by exact run and episode identity', () => {
  const rows = [
    workflow('1', { productionRunId: 'run-a', status: 'completed' }),
    workflow('2', { productionRunId: 'run-a', status: 'completed' }),
    workflow('3', { productionRunId: 'run-a', episodeId: 'episode-2' }),
    workflow('4', { productionRunId: 'run-b' }),
  ]

  const entries = buildTaskListEntries(rows)

  assert.equal(entries.length, 3)
  const firstEpisode = entries.find((entry) => entry.key === 'production-run:run-a:episode:episode-1')
  assert.ok(firstEpisode && firstEpisode.kind === 'group')
  assert.equal(firstEpisode.groupKind, 'production_run_episode')
  assert.equal(firstEpisode.unitCount, 2)
  assert.equal(firstEpisode.attemptCount, 2)
  assert.equal(firstEpisode.completedCount, 2)
  assert.equal(firstEpisode.status, 'completed')
  assert.equal(firstEpisode.completionPercent, 100)
})

test('missing ProductionRun identity is never guessed from a shared episode', () => {
  const first = workflow('1', { productionRunId: undefined })
  const second = workflow('2', { productionRunId: undefined })
  const missingEpisode = workflow('3', { productionRunId: 'run-a', episodeId: undefined })

  assert.equal(taskListGroupKey(first), '')
  assert.equal(taskListGroupKey(second), '')
  assert.equal(taskListGroupKey(missingEpisode), '')
  assert.deepEqual(
    buildTaskListEntries([first, second, missingEpisode]).map((entry) => entry.kind),
    ['task', 'task', 'task'],
  )
})

test('legacy batch remains one group and retains every retry attempt', () => {
  const rows = [
    workflow('old-1', {
      batchId: 'legacy-batch', batchIndex: 1, batchSize: 2,
      status: 'failed', createdAt: '2026-08-24T01:00:00Z', shotId: 'shot-1',
    }),
    workflow('retry-1', {
      batchId: 'legacy-batch', batchIndex: 1, batchSize: 2,
      status: 'completed', createdAt: '2026-08-24T01:10:00Z', shotId: 'shot-1',
    }),
    workflow('2', {
      batchId: 'legacy-batch', batchIndex: 2, batchSize: 2,
      status: 'running', createdAt: '2026-08-24T01:01:00Z', shotId: 'shot-2',
    }),
  ]

  const [entry] = buildTaskListEntries(rows)

  assert.ok(entry && entry.kind === 'group')
  assert.equal(entry.groupKind, 'legacy_batch')
  assert.equal(entry.attemptCount, 3)
  assert.equal(entry.tasks.length, 3)
  assert.equal(entry.currentTasks.length, 2)
  assert.equal(entry.completedCount, 1)
  assert.equal(entry.activeCount, 1)
  assert.equal(entry.failedCount, 0)
  assert.equal(entry.status, 'running')
  assert.equal(entry.completionPercent, 50)
})

test('ProductionRun variants are distinct current units while retries remain audit children', () => {
  const rows = [
    workflow('v1-old', {
      productionRunId: 'run-a', productionVariantNumber: 1, shotId: 'shot-1',
      status: 'failed', createdAt: '2026-08-24T01:00:00Z',
    }),
    workflow('v1-retry', {
      productionRunId: 'run-a', productionVariantNumber: 1, shotId: 'shot-1',
      status: 'completed', createdAt: '2026-08-24T01:05:00Z',
    }),
    workflow('v2', {
      productionRunId: 'run-a', productionVariantNumber: 2, shotId: 'shot-1',
      status: 'failed', createdAt: '2026-08-24T01:02:00Z',
    }),
  ]

  const [entry] = buildTaskListEntries(rows)

  assert.ok(entry && entry.kind === 'group')
  assert.equal(entry.tasks.length, 3)
  assert.equal(entry.unitCount, 2)
  assert.equal(entry.completedCount, 1)
  assert.equal(entry.failedCount, 1)
  assert.equal(entry.status, 'failed')
})

test('parent filters and counts use aggregate entries instead of child task counts', () => {
  const rows = [
    workflow('1', { productionRunId: 'done-run', status: 'completed' }),
    workflow('2', { productionRunId: 'done-run', status: 'completed' }),
    workflow('3', { productionRunId: 'active-run', status: 'running' }),
    workflow('4', { productionRunId: undefined, episodeId: undefined, status: 'failed' }),
  ]
  const entries = buildTaskListEntries(rows)

  assert.deepEqual(taskListFilterCounts(entries), {
    all: 3,
    active: 1,
    completed: 1,
    failed: 1,
  })
})

test('a paused whole-episode group remains paused while still matching the active filter', () => {
  const [entry] = buildTaskListEntries([
    workflow('1', { productionRunId: 'paused-run', status: 'paused' }),
    workflow('2', { productionRunId: 'paused-run', status: 'paused' }),
  ])

  assert.ok(entry && entry.kind === 'group')
  assert.equal(entry.status, 'paused')
  assert.equal(taskListFilterCounts([entry]).active, 1)
})
