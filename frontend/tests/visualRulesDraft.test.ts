import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'

test('legacy visual-rules draft writer stays absent from the UI', () => {
  assert.equal(existsSync(new URL('../src/components/GlobalVisualRulesDraft.vue', import.meta.url)), false)
  const studio = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(studio, /GlobalVisualRulesDraft|visual-rules-draft/)
})
