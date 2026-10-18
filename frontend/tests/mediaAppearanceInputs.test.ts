import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url))
const directory = () => ({ primary_inputs: [{ input_key: 'reference', target_key: 'actual.image.port' }], difference_sources: [
  { kind: 'manual_channel', key: 'positive_prompt' }, { kind: 'logical_input', key: 'brief' },
  { kind: 'structured_field', key: 'structured', field_key: 'exact_change' },
  { kind: 'reference_field', key: 'units', field_key: 'unit_direction' },
] })
async function setup(context: any) {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  context.after(() => vite.close())
  return { module: await vite.ssrLoadModule('/src/services/mediaAppearanceInputs.ts'),
    component: (await vite.ssrLoadModule('/src/components/MediaAppearanceInputDirectory.vue')).default }
}

test('exact input catalog is detached metadata, preserving original source identities only', async context => {
  const { module } = await setup(context), value = directory()
  const result = module.readMediaAppearanceInputOptions(value)
  assert.deepEqual(result, value); assert.notEqual(result, value)
  result.primary_inputs[0].input_key = 'changed'; assert.equal(value.primary_inputs[0].input_key, 'reference')
  assert.deepEqual(module.readMediaAppearanceInputOptions({ primary_inputs: [], difference_sources: [] }), { primary_inputs: [], difference_sources: [] })
})

test('missing unknown duplicate or body-bearing metadata fails instead of inventing input choices', async context => {
  const { module } = await setup(context)
  const invalid = [undefined, null, {}, { ...directory(), ready: true }, { ...directory(), primary_inputs: null },
    { ...directory(), primary_inputs: [...directory().primary_inputs, ...directory().primary_inputs] },
    { ...directory(), primary_inputs: [{ input_key: '', target_key: 'image' }] },
    { ...directory(), difference_sources: [...directory().difference_sources, directory().difference_sources[0]] }]
  for (const source of [{ kind: 'unknown', key: 'x' }, { kind: 'logical_input', key: 'x', text: 'body' },
    { kind: 'reference_field', key: 'units', field_key: 'f', unit_key: 'invented' },
    { kind: 'structured_field', key: 'json', field_key: 'f', path: ['chosen'] },
    { kind: 'structured_field', key: 'json' }, { kind: 'logical_input', key: ' ' }]) {
    invalid.push({ ...directory(), difference_sources: [source] } as any)
  }
  for (const value of invalid) assert.throws(() => module.readMediaAppearanceInputOptions(value), /外观输入目录不完整/)
})

test('read-only directory explains four source kinds, original units and limits without an action', async context => {
  const { module, component } = await setup(context)
  const html = await renderToString(createSSRApp(() => h(component, { options: module.readMediaAppearanceInputOptions(directory()), labels: { reference: { label: '主图参考位置' } } })))
  for (const text of ['主图参考位置', 'actual.image.port', '原人工通道', '策略正文输入', '结构化直通文字字段', '参考单元贡献字段', '确切单元', '不是可运行']) assert.ok(html.includes(text), text)
  assert.doesNotMatch(html, /<button|<input|<select|<textarea/)
})

test('empty catalog distinguishes output capability from actual primary and text inputs', async context => {
  const { component } = await setup(context)
  const html = await renderToString(createSSRApp(() => h(component, { options: { primary_inputs: [], difference_sources: [] }, labels: {} })))
  assert.match(html, /图片输出能力不等于可接收主图/)
  assert.match(html, /辅助上下文不能代替差异正文/)
})
