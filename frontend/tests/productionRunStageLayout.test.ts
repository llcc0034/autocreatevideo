import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const css = readFileSync(new URL('../src/platform.css', import.meta.url), 'utf8')
const view = readFileSync(new URL('../src/views/ProductionRunView.vue', import.meta.url), 'utf8')

test('stage aggregate is not presented as one tasks retry count', () => {
  assert.match(view, /阶段执行计数 \{\{ stage.attemptCount \}\}/)
  assert.match(view, /非单任务重试次数/)
  assert.doesNotMatch(view, /第 \{\{ stage.attemptCount \}\} 次尝试/)
})

test('expanded production stage spans the grid that contains its real operation forms', () => {
  assert.match(view, /expanded:expandedStageId===stage.id/)
  assert.match(css, /\.run-stage-strip>article\.expanded\{grid-column:1\/-1\}/)
  assert.match(css, /\.run-stage-strip\{display:grid;grid-template-columns:repeat\(auto-fit,minmax\(150px,1fr\)\)/)
  assert.doesNotMatch(css, /\.run-stage-strip\{display:flex/)
  assert.doesNotMatch(css, /\.run-stage-strip[^{}]*article\{min-width:(?:150px|84vw)/)
})

test('stage cards and reason styling do not leak into nested fulfillment and text results', () => {
  assert.match(css, /\.run-stage-strip>article\{/)
  assert.match(css, /\.run-stage-details>p\{/)
  assert.doesNotMatch(css, /\.run-stage-strip article[.{]/)
  assert.doesNotMatch(css, /\.run-stage-details p\{/)
})
