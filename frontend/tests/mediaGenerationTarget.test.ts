import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { readMediaGenerationTarget } from '../src/services/mediaGenerationTarget.ts'
import { targetFixture, versionId } from './fixtures/media-generation-target-data.ts'

test('absent generation target stays absent; no inference from inputs or current settings', () => {
  assert.deepEqual(readMediaGenerationTarget({ inputs: { role: 'primary' }, bible_target: {} }, versionId), { kind: 'missing' })
})
test('explicit empty setting and undeclared role are not automatic primary or human adoption', () => {
  const result = readMediaGenerationTarget({ media_generation_target: targetFixture() }, versionId)
  assert.equal(result.kind, 'recorded')
  if (result.kind !== 'recorded') return
  assert.equal(result.target.setting, null)
  assert.equal(result.target.appearance, null)
  assert.deepEqual(result.target.candidates, { count: 2, evaluator: null })
})
test('primary and variant expose exact immutable setting, primary version and input position', () => {
  for (const role of ['primary', 'variant'] as const) {
    const raw = targetFixture(role, true), before = JSON.stringify(raw)
    const result = readMediaGenerationTarget({ media_generation_target: raw }, versionId)
    assert.equal(result.kind, 'recorded')
    if (result.kind !== 'recorded') continue
    assert.equal(result.target.appearance?.role, role)
    assert.equal(result.target.setting?.revision, 3)
    assert.equal(result.target.setting?.revisionId, raw.setting!.setting_revision_id)
    assert.equal(result.target.appearance?.primary?.versionId ?? null, raw.appearance!.primary?.version_id ?? null)
    assert.equal(result.target.appearance?.difference?.key ?? null, role === 'variant' ? 'positive_prompt' : null)
    assert.equal(JSON.stringify(raw), before)
  }
})
test('invalid, mismatched and incomplete targets fail visibly, never return defaults', () => {
  const base = targetFixture('variant', true)
  const invalid: unknown[] = [null, [], {}, { ...base, schema_version: 2 }, { ...base, media_type: 'text' }, { ...base, media_type: ['image'] },
    { ...base, source_mode: 'existing' }, { ...base, execution_instance_version_id: 'latest' },
    { ...base, definition_sha256: 'unknown' }, { ...base, setting: undefined },
    { ...base, candidates: { count: 0, evaluator: null } }, { ...base, candidates: { count: true, evaluator: null } },
    { ...base, candidates: { count: 1, evaluator: 'human' } }, { ...base, appearance: null },
    { ...base, appearance: { ...base.appearance, role: 'other' } },
    { ...base, appearance: { ...base.appearance, primary: null } },
    { ...base, appearance: { ...base.appearance, primary_input: { input_key: 'x' } } },
    ...[undefined, null, {}, { kind: 'literal', key: 'new text', value_sha256: 'a'.repeat(64) },
      { kind: 'manual_channel', key: 'positive_prompt', value_sha256: 'bad' },
      { kind: 'manual_channel', key: 'positive_prompt', value_sha256: 'a'.repeat(64), text: 'duplicate' }]
      .map(difference => ({ ...base, appearance: { ...base.appearance, difference } })),
    { ...base, appearance: { ...base.appearance, setting: null } },
    { ...base, appearance: { ...base.appearance, role: 'primary' } }]
  for (const value of invalid) assert.equal(readMediaGenerationTarget({ media_generation_target: value } as any, versionId).kind, 'invalid')
  assert.equal(readMediaGenerationTarget({ media_generation_target: base }, null).kind, 'invalid')
  assert.equal(readMediaGenerationTarget({ media_generation_target: base }, '22222222-2222-4222-8222-222222222222').kind, 'invalid')
})
test('all recorded media kinds use only the recorded candidate policy', () => {
  for (const media_type of ['image', 'audio', 'video']) {
    const result = readMediaGenerationTarget({ media_generation_target: { ...targetFixture(), media_type,
      candidates: { count: 4, evaluator: 'first_succeeded' } } }, versionId)
    assert.equal(result.kind, 'recorded')
    if (result.kind === 'recorded') assert.equal(result.target.candidates.evaluator, 'first_succeeded')
  }
})
test('structured difference requires exact unit and field, never arbitrary selector or copied text', () => {
  const base = targetFixture('variant', true)
  const difference = { kind: 'reference_field', key: 'units', unit_key: 'second', field_key: 'unit_description', value_sha256: 'e'.repeat(64) }
  const read = (value: unknown) => readMediaGenerationTarget({ media_generation_target: {
    ...base, appearance: { ...base.appearance, difference: value } } } as any, versionId)
  const result = read(difference)
  assert.equal(result.kind, 'recorded')
  if (result.kind === 'recorded') assert.deepEqual(result.target.appearance?.difference, {
    kind: 'reference_field', key: 'units', unitKey: 'second', fieldKey: 'unit_description', sha256: 'e'.repeat(64) })
  for (const bad of [{ ...difference, unit_key: '' }, { ...difference, field_key: 'x'.repeat(192) },
    { ...difference, field_key: undefined }, { ...difference, selector: 'shot.action' }, { ...difference, text: 'copy' },
    { ...difference, kind: 'logical_input' }]) assert.equal(read(bad).kind, 'invalid')
})
test('structured passthrough references only the published field, never an editable path', () => {
  const base = targetFixture('variant', true)
  const difference = { kind: 'structured_field', key: 'change', field_key: 'exact_change', value_sha256: 'f'.repeat(64) }
  const read = (value: unknown) => readMediaGenerationTarget({ media_generation_target: {
    ...base, appearance: { ...base.appearance, difference: value } } } as any, versionId)
  const result = read(difference)
  assert.equal(result.kind, 'recorded')
  if (result.kind === 'recorded') assert.deepEqual(result.target.appearance?.difference, {
    kind: 'structured_field', key: 'change', fieldKey: 'exact_change', sha256: 'f'.repeat(64) })
  for (const bad of [{ ...difference, field_key: '' }, { ...difference, field_key: undefined },
    { ...difference, path: ['body'] }, { ...difference, unit_key: 'second' }, { ...difference, text: 'copy' }]) {
    assert.equal(read(bad).kind, 'invalid')
  }
})
test('real summary renders evidence boundaries, unknown state, exact ids and no write controls', async () => {
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent',
    appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const Panel = (await vite.ssrLoadModule('/src/components/MediaGenerationTargetSummary.vue')).default
    const render = (resolvedInputs: object) => renderToString(createSSRApp({ render: () => h(Panel, { resolvedInputs, versionId }) }))
    const variant = await render({ media_generation_target: targetFixture('variant', true) })
    for (const text of ['变体基准', '确切设定修订 3', 'selected_primary_image', 'exact-primary-item',
      '00000000-0000-4000-8000-000000000003', '不代表模型已收到请求', '不是人工审核记录',
      '差异来源', '原请求人工提示词通道', 'positive_prompt', '差异文字摘要']) assert.ok(variant.includes(text), text)
    const raw = targetFixture('variant', true)
    raw.appearance!.difference!.kind = 'logical_input'
    const logical = await render({ media_generation_target: raw })
    assert.match(logical, /原解析文字输入（策略正文映射）/)
    const structured = await render({ media_generation_target: { ...raw, appearance: { ...raw.appearance,
      difference: { kind: 'reference_field', key: 'units', unit_key: 'second', field_key: 'unit_description', value_sha256: 'e'.repeat(64) } } } })
    for (const text of ['原结构化参考字段', 'second', 'unit_description', '不是已选辅助结果']) assert.ok(structured.includes(text), text)
    const passthrough = await render({ media_generation_target: { ...raw, appearance: { ...raw.appearance,
      difference: { kind: 'structured_field', key: 'change', field_key: 'exact_change', value_sha256: 'f'.repeat(64) } } } })
    for (const text of ['原结构化直通字段', 'exact_change', '路径由本次确切策略指定', '不是另填的差异正文']) assert.ok(passthrough.includes(text), text)
    assert.doesNotMatch(variant, /<button|<input|<form/)
    const missing = await render({})
    assert.doesNotMatch(missing, /本次媒体生成目标|主图|生成成功/)
    const malformed = await render({ media_generation_target: null })
    assert.match(malformed, /role="alert"/)
    assert.doesNotMatch(malformed, /计划 .* 个候选|未配置自动选择/)
    const undeclared = await render({ media_generation_target: targetFixture() })
    assert.match(undeclared, /未声明主图\/变体/)
    assert.match(undeclared, /本次目标未关联设定/)
  } finally { await vite.close() }
})
