import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('prompt rule flow presents saved and draft semantics without resolving content', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const helpers = await vite.ssrLoadModule('/src/services/promptPrimitiveConfiguration.ts')
  const { default: Flow } = await vite.ssrLoadModule('/src/components/PromptRuleFlow.vue')
  const options = { channels: [{ value: 'positive_prompt', label: '正向通道' }], sourceSelectors: [], referenceRenderingFields: [],
    sourceProjectionPrimitives: [{ value: 'configured.primitive', label: '目录原语', engineKey: 'configured_engine', renderingTemplateKeys: ['text', 'empty', 'missing'] }] }
  const profile = { id: 'exact-profile', sourceProjectionConfig: { primitive_options: { configured_engine: { rendering: { templates: { text: '林岚🙂 {label} <script>literal</script>', empty: '' } } } } } }
  const rule = { id: 'r', profileVersion: profile.id, fieldKey: 'custom_field', fieldLabel: '自定义字段', sourceSelector: 'projection.output', projectionPrimitive: 'configured.primitive', generationMode: 'translate_only', generationOutputSemantics: 'contribution', generationSourceSelector: 'request.structured_input', channel: 'positive_prompt', position: 7 }
  const render = (overrides: any = {}) => renderToString(createSSRApp({ render: () => h(Flow, { profile, rule, options, ...overrides }) }))
  await context.test('translation final and contribution have distinct actual paths', async () => {
    const contribution = await render()
    for (const text of ['先按语言策略翻译', '作为贡献按模板拼接', '目录原语', '正向通道', '顺序 7', '已留空', '未保存', '林岚🙂']) assert.ok(contribution.includes(text), text)
    assert.match(contribution, /&lt;script&gt;literal&lt;\/script&gt;/)
    const final = await render({ rule: { ...rule, generationOutputSemantics: 'final' } })
    assert.match(final, /译文直接作为最终字段值/)
    assert.doesNotMatch(final, /此路径关联的文案模板|3 · 原语合成|林岚🙂/)
    assert.doesNotMatch(contribution + final, /<(input|select|textarea|button|form|script)\b/)
  })
  await context.test('unknown modes and semantics never imply a valid path', async () => {
    assert.match(await render({ rule: { ...rule, generationMode: 'unknown' } }), /不能推断处理路径/)
    assert.match(await render({ rule: { ...rule, generationOutputSemantics: '' } }), /不能推断后续路径/)
    const missing = await render({ options: undefined })
    assert.match(missing, /不能按字段名猜测/)
    assert.doesNotMatch(missing, /林岚🙂/)
  })
  await context.test('reverse template usage is exact-version and excludes final results and matching names', () => {
    const rows = [rule, { ...rule, id: 'final', generationOutputSemantics: 'final' }, { ...rule, id: 'other', profileVersion: 'other' }, { ...rule, id: 'other-primitive', projectionPrimitive: 'text' }]
    assert.deepEqual(helpers.templateRuleUsages('text', profile.id, rows, options).map((x: any) => x.id), ['r'])
    assert.deepEqual(helpers.templateRuleUsages('text', '', rows, options), [])
    assert.deepEqual(helpers.templateRuleUsages('text', profile.id, rows), [])
  })
  await context.test('draft semantics change reactively and preserve all source values', async () => {
    const props = reactive({ rule: { ...rule }, profile, options, draft: true }); let state: any
    const before = JSON.stringify(profile)
    await renderToString(createSSRApp({ setup() { state = Flow.setup(props, { expose() {} }); return () => h('div') } }))
    assert.equal(state.path.value.active, true)
    props.rule.generationOutputSemantics = 'final'
    assert.equal(state.path.value.active, false)
    props.rule.generationMode = 'passthrough'
    assert.equal(state.path.value.active, true)
    props.rule.sourceSelector = 'fixed.value'
    assert.equal(state.path.value.active, false)
    assert.equal(JSON.stringify(profile), before)
    assert.match(await render({ draft: true }), /当前未保存设置/)
  })
  await context.test('structured contribution uses its explicit branch without a conflicting incompatibility notice', async () => {
    const { default: Settings } = await vite.ssrLoadModule('/src/components/PromptProfileSettings.vue'); let state: any
    await renderToString(createSSRApp({ setup() { state = Settings.setup({}, { expose() {} }); return () => h('div') } }))
    state.profiles.value = [{ id: 'p', status: 'draft', sourceProjectionKey: 'declarative_v1' }]; state.selectedProfileId.value = 'p'
    state.options.value.sourceSelectors = [{ value: 'request.structured_input' }]
    state.options.value.sourceProjectionPrimitives = [{ value: 'exact', contributionSourceSelectors: ['destination'] }]
    state.options.value.generationSourceSelectors = [{ value: 'request.structured_input', status: 'active', generationModes: ['translate_only'] }]
    Object.assign(state.ruleForm, { sourceSelector: 'projection.output', projectionPrimitive: 'exact', generationMode: 'translate_only', generationOutputSemantics: 'contribution', generationSourceSelector: 'request.structured_input' })
    assert.equal(state.structuredContributionAvailable.value, true)
    assert.equal(state.generationSourceIncompatible.value, false)
    state.ruleForm.projectionPrimitive = 'not-registered'
    assert.equal(state.generationSourceIncompatible.value, true)
  })
  await context.test('processing facts use exact generation catalog, structure path and configured language', async () => {
    const enhanced = { ...rule, sourcePath: ['same.name', 0, '原文'], languagePolicy: 'bilingual:en+zh-Hant', generationInstruction: '只翻译 <输入>。', generationContextSelectors: ['project.style', 'unknown.context'] }
    const opts = { ...options, sourceSelectors: [{ value: 'request.structured_input', label: '普通读取入口' }], generationSourceSelectors: [{ value: 'request.structured_input', label: '辅助确切来源' }], generationContextSelectors: [{ value: 'project.style', label: '项目风格' }] }
    const before = JSON.stringify({ enhanced, opts, profile })
    const html = await render({ rule: enhanced, profile: { ...profile, inputStructureVersion: 'exact-structure-v2' }, options: opts })
    for (const value of ['辅助确切来源', 'exact-structure-v2', '确切字段路径', '列表 · 3 项', '翻译目标语言', 'bilingual:en+zh-Hant', '只翻译 &lt;输入&gt;。', '项目风格', 'unknown.context', '不因语言设置而翻译']) assert.ok(html.includes(value), value)
    assert.doesNotMatch(html, /普通读取入口/)
    assert.equal(JSON.stringify({ enhanced, opts, profile }), before)
  })
  await context.test('non-assisted modes do not advertise dormant helper settings as active inputs', async () => {
    for (const generationMode of ['passthrough', 'server_locked']) {
      const html = await render({ rule: { ...rule, sourceSelector: 'fixed.value', generationMode, languagePolicy: 'preserve_source', generationInstruction: 'DORMANT-INSTRUCTION', generationContextSelectors: ['DORMANT-CONTEXT'], sourcePath: ['OLD-PATH'] } })
      assert.match(html, /本方式不派发字段辅助任务/)
      assert.doesNotMatch(html, /辅助处理会收到什么|DORMANT|OLD-PATH|输入结构版本/)
    }
  })
  await context.test('absent and empty processing facts stay distinct without invented defaults', async () => {
    const missing = await render({ rule: { ...rule, languagePolicy: '', generationContextSelectors: undefined } })
    assert.match(missing, /额外信息字段未提供/); assert.match(missing, /字段缺失/); assert.match(missing, /未选择语言策略/)
    const empty = await render({ rule: { ...rule, generationContextSelectors: [], generationInstruction: '', sourcePath: [] } })
    assert.match(empty, /未选择额外信息/); assert.match(empty, /空文本/)
    assert.doesNotMatch(empty, /项目风格|默认英文/)
  })
})
