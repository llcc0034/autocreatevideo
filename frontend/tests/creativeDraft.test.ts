import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'

const source = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('legacy creative-draft write client and workbench stay removed', () => {
  assert.equal(existsSync(new URL('../src/services/creativeDraftRepository.ts', import.meta.url)), false)
  assert.equal(existsSync(new URL('../src/components/CreativeDraftWorkbench.vue', import.meta.url)), false)
  assert.doesNotMatch(source('../src/views/SeriesStudioView.vue'), /CreativeDraftWorkbench|creative-drafts\/generate|applyCreativeDraft/)
})
