import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'

test('legacy shot-detail draft writers and UI stay removed', () => {
  assert.equal(existsSync(new URL('../src/services/shotDetailDraftRepository.ts', import.meta.url)), false)
  assert.equal(existsSync(new URL('../src/components/ShotDetailDraftPanel.vue', import.meta.url)), false)
  assert.equal(existsSync(new URL('../src/components/EpisodeDetailDraftModal.vue', import.meta.url)), false)
  const storyboard = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(storyboard, /ShotDetailDraftPanel|EpisodeDetailDraftModal|shot-detail-draft/)
})
