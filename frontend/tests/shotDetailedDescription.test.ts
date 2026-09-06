import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('manual storyboard exposes the model-agnostic detail source field', () => {
  const storyboard = readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')
  assert.match(storyboard, /v-model="selectedShot\.detailedDescription"/)
  assert.match(storyboard, /逐镜高细节事实（提示词字段的上游内容）/)
  assert.match(storyboard, /detailedDescription:''/)
})
