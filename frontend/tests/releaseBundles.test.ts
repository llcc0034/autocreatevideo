import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const id = (n: number) => `10000000-0000-0000-0000-${String(n).padStart(12, '0')}`
const selection = { shots: [{ shot_id: id(4), artifact_id: id(5) }], episodes: [{ episode_id: id(6), artifact_id: id(7) }], series_artifact_id: null }
const raw = { id: id(1), project: id(2), production_run: id(3), version: 1, title: 'Release', notes: '',
  status: 'sealed', verification_status: 'verified', selection, manifest_sha256: 'a'.repeat(64),
  sealed_at: '2026-09-10T10:00:00Z', released_at: null, created_at: '2026-09-10T10:00:00Z', updated_at: '2026-09-10T10:00:00Z',
  manifest: { schema: 'autoprompt-production-release-bundle-v3', production_run_id: id(3),
    assemblies: [{ scope: 'episode', episode_id: id(6), generated_artifact_id: id(7), assembly_id: id(8) }] } }
async function modules(fn) {
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try { await fn(await vite.ssrLoadModule('/src/services/releaseBundles.ts'), vite) } finally { await vite.close() }
}

test('release reader preserves exact artifact members and its own ProductionRun', () => modules(m => {
  const result = m.adaptReleaseBundle(raw)
  assert.equal(result.productionRunId, id(3))
  assert.deepEqual(result.selection, { shots: [{ shotId: id(4), artifactId: id(5) }], episodes: [{ episodeId: id(6), artifactId: id(7) }] })
  assert.deepEqual(result.assemblySources, [{ episodeId: id(6), artifactId: id(7), assemblyId: id(8) }])
  assert.equal(result.releasedAt, undefined)
  for (const status of ['invalid', 'draft']) {
    const record = m.adaptReleaseBundle({ ...raw, verification_status: status, manifest: {}, manifest_sha256: '', sealed_at: null })
    assert.deepEqual(record.selection, result.selection)
    assert.equal(record.assemblySources, undefined)
    assert.equal(record.verificationStatus, status)
  }
}))

test('release reader rejects missing, old, duplicate and mismatched facts instead of presenting empty membership', () => modules(m => {
  for (const change of [{ production_run: undefined }, { production_run: '' }, { version: 0 }, { verification_status: undefined },
    { selection: undefined }, { selection: { shots: [], episodes: [], series_assembly_id: null } },
    { selection: { ...selection, shots: [{ shot_id: id(4), task_id: id(5), output_index: 0 }] } },
    { selection: { ...selection, episodes: [{ episode_id: id(6), assembly_id: id(8) }] } },
    { selection: { ...selection, shots: [selection.shots[0], selection.shots[0]] } },
    { manifest: { ...raw.manifest, schema: 'autoprompt-release-bundle-v2' } },
    { manifest: { ...raw.manifest, production_run_id: id(9) } },
    { manifest: { ...raw.manifest, assemblies: [] } },
    { manifest: { ...raw.manifest, assemblies: [{ ...raw.manifest.assemblies[0], generated_artifact_id: id(9) }] } },
    { manifest: { ...raw.manifest, assemblies: [{ ...raw.manifest.assemblies[0], scope: 'series' }] } }]) {
    assert.throws(() => m.adaptReleaseBundle({ ...raw, ...change }), /发布包/)
  }
}))

test('release page uses the formal reader and direct run identity without inferring it from another list', async () => {
  const repo = await readFile(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const view = await readFile(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  assert.ok(repo.includes("import { adaptReleaseBundle } from './releaseBundles'"))
  assert.ok(repo.includes('rows.map(adaptReleaseBundle)'))
  assert.ok(view.includes('bundle.productionRunId'))
  assert.equal(view.includes('productionRunForRelease'), false)
  assert.equal(view.includes('seriesAssemblyId'), false)
})
