import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'

test('legacy series-concept generator stays absent from the product surface', () => {
  assert.equal(existsSync(new URL('../src/services/seriesConceptDraftRepository.ts', import.meta.url)), false)
  assert.equal(existsSync(new URL('../src/components/SeriesConceptDraftWorkbench.vue', import.meta.url)), false)
  const source = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /SeriesConceptDraftWorkbench|series-concept-drafts\/generate/)
})
