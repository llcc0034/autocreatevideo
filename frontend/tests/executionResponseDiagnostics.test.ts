import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

async function component(context: any, path = 'ExecutionResponseDiagnostics') {
  const vite = await createServer({ logLevel: 'silent', server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  return (await vite.ssrLoadModule(`/src/components/${path}.vue`)).default
}
const relation = { path: 'value.episodes[0].shots[0].references[0].variant_key',
  source_path: 'source.chapters[0].frames[0].uses[0].variant_key', origin_kind: 'path', code: 'variant_unknown', message: '变体未声明' }
const diagnostic = () => ({ structure_errors: { [relation.path]: [relation.message], [relation.source_path]: [relation.message] }, relation_errors: [relation] })

test('source and semantic aliases render one issue with both locations', async context => {
  const html = await renderToString(createSSRApp(await component(context), { diagnostics: diagnostic() }))
  assert.match(html, /引用 · 1 项/); assert.match(html, /source.chapters/); assert.match(html, /对应业务位置：value.episodes/)
  assert.equal(html.match(/>变体未声明<\/p>/g)?.length, 1)
})
test('generic type and JSON errors render without a script-specific requirement', async context => {
  const html = await renderToString(createSSRApp(await component(context), { diagnostics: { structure_errors: { 'response.count': ['必须为整数'], response: ['JSON不完整'] } } }))
  assert.match(html, /引用 · 2 项/); assert.match(html, /必须为整数/); assert.match(html, /JSON不完整/)
})
test('literal configuration is never labelled as a source-text field', async context => {
  const r = { path: 'value.scene_key', origin_kind: 'literal', message: '未声明场景' }
  const html = await renderToString(createSSRApp(await component(context), { diagnostics: { structure_errors: { [r.path]: [r.message] }, relation_errors: [r] } }))
  assert.match(html, /配置字面值（非原文字段）/); assert.doesNotMatch(html, /对应业务位置|source\./)
})
test('malformed diagnostic remains explicit and missing details do not claim success', async context => {
  const Component = await component(context)
  for (const diagnostics of [{ structure_errors: false }, { structure_errors: { path: 'bad' } }, { structure_errors: {}, relation_errors: false }]) {
    const html = await renderToString(createSSRApp(Component, { diagnostics }))
    assert.match(html, /字段诊断格式异常/); assert.match(html, /查看诊断记录/)
  }
  for (const diagnostics of [{}, { structure_errors: {} }]) {
    assert.doesNotMatch(await renderToString(createSSRApp(Component, { diagnostics })), /通过|无错误|需修正/)
  }
})
test('paths and messages are escaped instead of interpreted as markup', async context => {
  const html = await renderToString(createSSRApp(await component(context), { diagnostics: { structure_errors: { '<script>': ['<img src=x onerror=alert(1)>'] } } }))
  assert.doesNotMatch(html, /<script>|<img /); assert.match(html, /&lt;script&gt;/)
})
test('returned-response and preparation views use the same display implementation', async context => {
  const html = await renderToString(createSSRApp(await component(context, 'ExecutionReturnedText'), { response: {
    diagnostics: { ...diagnostic(), summary: '隔离校验失败' }, notices: [], fields: [], originalText: '{bad JSON',
  } }))
  assert.match(html, /返回内容字段诊断/); assert.match(html, /source.chapters/); assert.match(html, /bad JSON/)
  const workbench = await readFile(new URL('../src/components/ExecutionPreparationWorkbench.vue', import.meta.url), 'utf8')
  assert.match(workbench, /<ExecutionResponseDiagnostics v-if="!preparation.textResponseReview.confirmedAt"/)
})
