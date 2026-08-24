import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'

test('legacy script Bible candidate workbench and repository stay removed', () => {
  assert.equal(existsSync(new URL('../src/components/ScriptBibleCandidatesPanel.vue', import.meta.url)), false)
  assert.equal(existsSync(new URL('../src/services/scriptBibleCandidates.ts', import.meta.url)), false)
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const studio = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(repository, /script-bible-candidates|sync-bibles|breakdown-script/)
  assert.doesNotMatch(studio, /ScriptBibleCandidatesPanel|scriptBible|sync-bibles|breakdown-script/)
})
