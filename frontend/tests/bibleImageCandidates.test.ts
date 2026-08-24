import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('Bible image candidates expose read-only history without create or promotion controls', () => {
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const studio = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const service = readFileSync(new URL('../src/services/bibleImageCandidates.ts', import.meta.url), 'utf8')
  assert.match(repository, /api\.get\(\`\/projects\/\$\{projectId\}\/bible-image-candidates\/\`\)/)
  assert.doesNotMatch(repository, /api\.post\([^\n]*bible-image-candidates|createBibleImageCandidate|promoteTaskOutputToAsset/)
  assert.doesNotMatch(studio, /repository\.(createBibleImageCandidate|promoteTaskOutputToAsset)/)
  assert.doesNotMatch(studio, /@click="(?:generateAllUnconfirmedBibleImageCandidates|generateBibleImageCandidate|promoteBibleImageOutput)/)
  assert.doesNotMatch(service, /bibleImageCandidateCreatePayload|CandidateRetryState|mergeBibleImagePromotion/)
  assert.match(studio, /只读历史/)
})
