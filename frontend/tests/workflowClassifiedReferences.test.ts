import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const server = () => createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true } })
const definition = () => ({
  node_roles: { receiver: { node_type: 'FutureReferenceNode', cardinality: 'one' } },
  asset_slots: ['images', 'videos', 'video_audios', 'audios'].map((key, index) => ({ key, media_kind: ['image', 'video', 'audio', 'audio'][index], dynamic: true, contract_source: 'workflow' })),
  projection: { kind: 'generic_classified', config: {
    capability: 'video.reference_to_video', conditioning_mode: 'ReferenceMode', target_role: 'receiver', prompt_template_keys: [], static_semantics: { 'audio.enabled': true },
    dynamic_groups: Object.fromEntries(['images', 'videos', 'video_audios', 'audios'].map((key, index) => [key, { prefix: `${key}.`, input_names: [], mode_input: ['entity_images', 'reference_videos', 'paired_video_audios', 'reference_audios'][index], minimum_items: 0, maximum_items: 3 }])),
  } },
})

test('classified source links resolve the same group, mode and quantity fields as the editor', async context => {
  const vite = await server(); context.after(() => vite.close())
  const { assetGroupForSlot } = await vite.ssrLoadModule('/src/services/workflowAssetContractSource.ts')
  const value = definition(), before = JSON.stringify(value)
  const group = assetGroupForSlot(value, 'video_audios')
  assert.equal(group.conditioning_mode, 'ReferenceMode')
  assert.equal(group.model_input, 'paired_video_audios')
  assert.equal(group.maximum_items, 3)
  assert.equal(group.configuration_path, 'projection.config.dynamic_groups')
  assert.equal(group.key, 'video_audios')
  assert.equal(JSON.stringify(value), before)
})

test('classified editor exposes four configurable groups without model-specific controls', async context => {
  const vite = await server(); context.after(() => vite.close())
  const { createSSRApp, h } = await import('vue')
  const { renderToString } = await import('vue/server-renderer')
  const { default: Editor } = await vite.ssrLoadModule('/src/components/WorkflowClassifiedReferenceEditor.vue')
  const html = await renderToString(createSSRApp({ render: () => h(Editor, {
    modelValue: definition(), disabled: true, modelProfiles: [],
    options: { definitionEditor: { choices: { contractSources: [{ value: 'workflow', label: 'Projector 中配置' }, { value: 'model_profile', label: '模型版本合同' }] } } },
  }) }))
  for (const label of ['参考图片', '参考视频', '视频配套音频', '独立参考音频', '最少数量', '最多数量', '按视频序号', '动态端口完整前缀']) assert.ok(html.includes(label), label)
  assert.match(html, /disabled/)
  assert.doesNotMatch(html, /minimax_h3|H3 Ref2VA|固定 4 步/)
})

test('quantity source follows the selected model, reads lazily and rejects late responses', async context => {
  const vite = await server(); context.after(() => vite.close())
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const original = platformApi.getModelProfile
  context.after(() => { platformApi.getModelProfile = original })
  const pending: any[] = []
  platformApi.getModelProfile = (id: string) => new Promise(resolve => pending.push({ id, resolve }))
  const { default: Component } = await vite.ssrLoadModule('/src/components/WorkflowAssetContractSource.vue')
  const { createRenderer, h, reactive, nextTick, ssrContextKey } = await import('vue')
  const renderer = createRenderer<any, any>({
    createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    insert: () => {}, remove: () => {}, setText: () => {}, setElementText: () => {},
    parentNode: () => null, nextSibling: () => null, patchProp: () => {},
  })
  const props = reactive({ slot: { contract_source: 'model_profile' }, definition: {}, initialModelProfileId: '' })
  let state: any
  const app = renderer.createApp({ setup() { state = Component.setup(props, { expose: () => {} }); return () => h('div') } })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount({}); context.after(() => app.unmount())
  props.initialModelProfileId = 'model-a'; await nextTick()
  assert.equal(state.selectedId.value, 'model-a'); assert.equal(pending.length, 0)
  state.expanded.value = true; await nextTick()
  assert.equal(pending[0].id, 'model-a')
  props.initialModelProfileId = 'model-b'; await nextTick()
  assert.equal(pending[1].id, 'model-b')
  pending[1].resolve({ id: 'model-b', specification: {} }); await Promise.resolve(); await nextTick()
  pending[0].resolve({ id: 'model-a', specification: {} }); await Promise.resolve(); await nextTick()
  assert.equal(state.profile.value.id, 'model-b')
  props.initialModelProfileId = ''; await nextTick()
  assert.equal(state.profile.value, null)
})

test('classified interval comparison exposes model and total bounds without changing either source', async context => {
  const vite = await server(); context.after(() => vite.close())
  const { classifiedQuantityComparison: compare } = await vite.ssrLoadModule('/src/services/workflowAssetContractSource.ts')
  const profile = { specification: { conditioning_contract: { modes: [{ key: 'Mode', input_kind: 'classified_references', inputs: {
    entity_images: { minimum_items: 0, maximum_items: 4 }, total_images: { minimum_items: 2, maximum_items: 4 },
  } }] } } }
  const group = { conditioning_mode: 'Mode', model_input: 'entity_images', minimum_items: 2, maximum_items: 3 }
  const slot = { contract_source: 'workflow' }
  const before = JSON.stringify({ profile, group, slot })
  const matched = compare(profile, group, slot)
  assert.equal(matched.state, 'matched')
  assert.deepEqual(matched.total, { minimum: 2, maximum: 4 })
  assert.deepEqual(matched.configured, { minimum: 2, maximum: 3 })
  assert.match(compare(profile, group, { contract_source: 'model_profile' }).message, /total_images.*2–4/)
  assert.match(compare(profile, { ...group, maximum_items: 5 }, slot).message, /模型输入 0–4/)
  assert.match(compare(profile, { ...group, minimum_items: true }, slot).message, /无效/)
  assert.equal(compare(null, group, slot).state, 'none')
  assert.match(compare(profile, { ...group, conditioning_mode: 'missing' }, slot).message, /未唯一声明/)
  assert.equal(JSON.stringify({ profile, group, slot }), before)
})
