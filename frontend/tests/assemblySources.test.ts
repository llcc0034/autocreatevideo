import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'

test('assembly detail preserves actual media count, zero and missing without old task fallback', async t => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false } })
  t.after(() => server.close())
  const { api } = await server.ssrLoadModule('/src/services/api.ts')
  const { repository } = await server.ssrLoadModule('/src/services/repository.ts')
  const get = api.get
  t.after(() => { api.get = get })
  let raw: Record<string, unknown> = { id: 'assembly', status: 'succeeded', scope: 'episode', source_media_count: 2 }
  const calls: unknown[] = []
  api.get = async (path: string) => { calls.push(path); return { data: raw } }
  assert.equal((await repository.getAssembly('assembly')).sourceMediaCount, 2)
  for (const ai of [true, false, null, undefined, 'true']) {
    raw = { ...raw, ai_generated: ai }
    assert.equal((await repository.getAssembly('assembly')).aiGenerated, typeof ai === 'boolean' ? ai : undefined)
  }
  for (const count of [0, undefined, -1, '2', true, 1.5]) {
    raw = { ...raw, source_media_count: count, source_tasks: ['obsolete-task'] }
    const result = await repository.getAssembly('assembly')
    assert.equal(result.sourceMediaCount, count === 0 ? 0 : undefined)
    assert.equal('sourceTaskIds' in result, false)
  }
  assert.ok(calls.every(path => path === '/assemblies/assembly/'))
  const error = new Error('assembly unavailable')
  api.get = async () => { throw error }
  await assert.rejects(repository.getAssembly('assembly'), error)
})

test('production studio labels actual adopted media and makes missing count explicit', () => {
  const source = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  assert.match(source, /sourceMediaCount === undefined/)
  assert.match(source, /来源数量未返回/)
  assert.match(source, /个已采用媒体/)
  assert.doesNotMatch(source, /sourceTaskIds/)
})
