import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

const root = fileURLToPath(new URL('..', import.meta.url))
const layer = (source: string, rule: any) => ({ source, rule: { type: 'integer', required: false, ...rule } })
const entry = {
  rule: { type: 'integer', required: true, minimum: 64, maximum: 512, alignments: [{ modulus: 32, remainder: 0 }], default: 352 },
  value_source: 'instance.instance-id.parameters',
  layers: [layer('model.model-id.media_constraints', { minimum: 32, maximum: 1024 }),
    layer('workflow.workflow-id.parameters', { minimum: 64, maximum: 768, default: 512 }),
    layer('instance.instance-id.parameters', { maximum: 512, default: 352 })],
}
const open = (View: any) => ({ ...View, setup(props: any, context: any) {
  const state = View.setup(props, context); state.expanded.value = true; return state
} })

test('parameter evidence shows server intersection and each original layer without merging or network', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const View = (await vite.ssrLoadModule('/src/components/ExecutionParameterEvidence.vue')).default
    const before = JSON.stringify(entry)
    const render = (view: any, props: any) => renderToString(createSSRApp({ render: () => h(view, props) }))
    const closed = await render(View, { entry })
    assert.match(closed, /64 ～ 512/); assert.match(closed, /合同默认 352/)
    assert.match(closed, /当前精确执行实例版本/); assert.match(closed, /3 层/)
    assert.doesNotMatch(closed, /32 ～ 1024|64 ～ 768/)
    const html = await render(open(View), { entry })
    for (const phrase of ['32 ～ 1024', '64 ～ 768', '未声明下限 ～ 512', '精确模型版本', '精确工作流版本', '只有默认值', '固定值不能被覆盖', '不是工作流节点/API 字段的物理路径']) assert.ok(html.includes(phrase), phrase)
    assert.doesNotMatch(html, /<input|<select|<textarea|<button/)
    assert.equal(JSON.stringify(entry), before)
    // A presentation layer must show, not recompute, the supplied effective rule.
    const recorded = await render(View, { entry: { ...entry, rule: { ...entry.rule, maximum: 384 } } })
    assert.match(recorded, /64 ～ 384/)
  } finally { await vite.close() }
})

test('missing provenance is explicit; defaults are not substituted for request or generated value sources', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const View = open((await vite.ssrLoadModule('/src/components/ExecutionParameterEvidence.vue')).default)
    const render = (props: any) => renderToString(createSSRApp({ render: () => h(View, props) }))
    for (const actualSource of ['request', 'preparation.generated', 'system.execution.resources.review_policy@rev-id', undefined]) {
      const html = await render({ entry, resolved: true, actualSource })
      assert.match(html, /本次值来源/); assert.doesNotMatch(html, /合同默认／固定值来源/)
      if (actualSource === undefined) assert.match(html, /来源记录缺失/)
      else if (actualSource === 'request') assert.match(html, /本次请求填写/)
      else if (actualSource === 'preparation.generated') assert.match(html, /首次准备时生成并保存/)
      else assert.match(html, /系统设置/)
    }
    for (const layers of [undefined, [], {}, [entry.layers[0], entry.layers[0]], [{ source: 'known', rule: {} }]]) {
      const html = await render({ entry: { ...entry, layers } })
      assert.match(html, /来源层记录缺失或格式不完整/); assert.match(html, /64 ～ 512/)
      assert.doesNotMatch(html, /3 层/)
    }
    assert.match(await render({ entry: null }), /生效约束记录缺失或不完整/)
    assert.match(await render({ entry: { rule: { type: 'boolean', required: false, const: false }, value_source: 'unknown.<script>', layers: [] } }), /固定值 false/)
    const escaped = await render({ entry: { ...entry, value_source: '<script>alert(1)</script>' } })
    assert.match(escaped, /&lt;script&gt;/); assert.doesNotMatch(escaped, /<script>/)
    const system = { rule: { type: 'integer', required: true, minimum: 1 }, value_source: null, layers: [layer('system.execution.resources.review_policy', { minimum: 1 })] }
    assert.match(await render({ entry: system }), /创建准备时读取系统配置并冻结/)
    assert.match(await render({ entry: { ...system, layers: [], rule: { ...system.rule, seed_policy: 'generate_once' } } }), /尚未生成种子/)
  } finally { await vite.close() }
})

test('contract and preparation evidence keep coupled constraints and actual values separate', async () => {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const Contract = open((await vite.ssrLoadModule('/src/components/ExecutionParameterContract.vue')).default)
    const Facts = open((await vite.ssrLoadModule('/src/components/ExecutionPreparationFacts.vue')).default)
    const render = (view: any, props: any) => renderToString(createSSRApp({ render: () => h(view, props) }))
    const intf = { parameters: { width: entry }, media_constraints: { resolution: { maximum_short_side: 352 } }, media_semantic_keys: { width: 'image.width' }, frame_policy: { alignment_modulus: 17 } }
    const html = await render(Contract, { interface: intf })
    for (const text of ['1 项', '单个参数满足范围，不代表', '模型媒体整体约束', '媒体语义', '工作流帧数与时长关系']) assert.ok(html.includes(text), text)
    assert.match(await render(Contract, { interface: {} }), /不能判定为没有参数/)
    assert.match(await render(Contract, { interface: { parameters: {} } }), /明确没有可调标量参数/)
    const saved = { inputs: { logical_values: {} }, parameters: { values: { width: 64, fps: 24 }, sources: { width: 'request', fps: 'workflow.static_semantics' }, contracts: { width: entry } } }
    const before = JSON.stringify(saved)
    const actual = await render(Facts, { resolvedInputs: saved })
    assert.match(actual, />64</); assert.match(actual, /本次请求填写/); assert.match(actual, /来自工作流冻结静态事实/)
    assert.doesNotMatch(actual, /合同默认／固定值来源|生效约束记录缺失/)
    assert.equal(JSON.stringify(saved), before)
  } finally { await vite.close() }
})
