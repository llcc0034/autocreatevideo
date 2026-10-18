import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { readSettingSource, sourceOriginLabel } from '../src/services/bibleVariants.ts'
import type { BibleVariantDetail } from '../src/services/bibleVariants.ts'

const detail = (): BibleVariantDetail => ({ id: 'appearance', project_id: 'project', character_id: 'character', scene_id: null, prop_id: null,
  name: '人物', reference_name: '人物·主图', revision: 2, current_decision: null, created_by_id: 1, created_at: 'stamp', updated_at: 'stamp',
  appearance_role: 'primary', source: { binding_id: 'binding', script_revision_id: 'script', entity_key: 'person', variant_key: null, declaration_sha256: 'a'.repeat(64) },
  primary_setting: null, setting: { id: 'setting', revision: 2, name: '人物', reference_name: '人物·主图', identity_anchor: '', appearance_anchor: '人工蓝衣',
    basis_asset_id: null, basis_asset_sha256: '', content_sha256: 'b'.repeat(64), created_by_id: 1, created_at: 'stamp', review: null,
    payload_schema_version: 2, source_declaration_sha256: '', primary_setting_revision_id: null } })
const evidence = () => {
  const basis = detail()
  return { appearance_id: basis.id, setting_revision_id: basis.setting.id, setting_sha256: basis.setting.content_sha256,
    source: basis.source, document_sha256: 'c'.repeat(64), fields: [
      { key: 'name', label: '名称', declaration_value: '人物', setting_value: '人物', origin: 'source_value', source_path: 'source.cast[0].name', changed_from_declaration: false },
      { key: 'reference_name', label: '引用名', declaration_value: '人物·主图', setting_value: '人物·主图', origin: 'configured_literal', source_path: null, changed_from_declaration: false },
      { key: 'identity_anchor', label: '共同身份', declaration_value: null, setting_value: '', origin: 'missing', source_path: null, changed_from_declaration: false },
      { key: 'appearance_anchor', label: '主图外观', declaration_value: '配置白衣', setting_value: '人工蓝衣', origin: 'configured_default', source_path: 'source.cast[0].look', changed_from_declaration: true },
    ] }
}

test('comparison retains absent, blank, source, literal, default and edited content distinctly', () => {
  const result = readSettingSource(evidence(), detail())
  assert.equal(result.fields[2].declaration_value, null)
  assert.equal(result.fields[2].setting_value, '')
  assert.equal(result.fields[3].changed_from_declaration, true)
  assert.equal(sourceOriginLabel('configured_literal'), '映射配置常量')
  assert.equal(sourceOriginLabel('configured_default'), '字段配置默认值')
  const manual = detail(); manual.source = null
  const value = evidence(); value.source = null; value.document_sha256 = null
  value.fields = value.fields.map(row => ({ ...row, declaration_value: null, origin: 'manual', source_path: null, changed_from_declaration: null }))
  assert.equal(readSettingSource(value, manual).fields[0].changed_from_declaration, null)
})

test('wrong revision, ownership, values, duplicate fields and false provenance cannot display success', () => {
  for (const mutate of [
    value => value.setting_revision_id = 'other', value => value.appearance_id = 'other', value => value.setting_sha256 = '0'.repeat(64),
    value => value.document_sha256 = null, value => value.source.entity_key = 'other', value => value.fields.pop(),
    value => value.fields[1].key = 'name', value => value.fields[0].setting_value = 'not viewed', value => value.fields[3].changed_from_declaration = false,
    value => value.fields[1].source_path = 'pretend', value => value.fields[0].source_path = null, value => value.fields[0].origin = 'invented',
    value => value.fields[2].declaration_value = '', value => value.fields[1].declaration_value = null,
  ]) {
    const value: any = evidence(); mutate(value)
    assert.throws(() => readSettingSource(value, detail()))
  }
})

test('source API pins the displayed revision and checks the returned immutable reference', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const { bibleVariantApi } = await vite.ssrLoadModule('/src/services/bibleVariantApi.ts')
  const calls: any[] = []
  api.get = async (url, options) => { calls.push({ url, options }); return { data: evidence() } }
  await bibleVariantApi.source(detail())
  assert.deepEqual(calls, [{ url: '/bible-variants/appearance/source/', options: { params: { revision: 2 } } }])
  api.get = async () => ({ data: { ...evidence(), setting_revision_id: 'other' } })
  await assert.rejects(bibleVariantApi.source(detail()))
})

test('comparison loads on demand, retries failures and discards old revision responses', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { bibleVariantApi } = await vite.ssrLoadModule('/src/services/bibleVariantApi.ts')
  const { default: Component } = await vite.ssrLoadModule('/src/components/BibleSettingSource.vue')
  let calls = 0, fail = true
  bibleVariantApi.source = async () => { calls++; if (fail) throw Error('隔离来源缺失'); return evidence() }
  const props = reactive({ detail: detail() })
  const renderer = createRenderer<any,any>({ createElement:()=>({}), createText:()=>({}), createComment:()=>({}),
    insert(){}, remove(){}, setText(){}, setElementText(){}, parentNode:()=>null, nextSibling:()=>null, patchProp(){} })
  let state: any
  const app = renderer.createApp({ setup() { state = Component.setup(props, { expose(){} }); return () => h('div') } })
  try {
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await nextTick()
    assert.equal(calls, 0)
    await state.load(); assert.match(state.error.value, /隔离来源缺失/); assert.equal(state.result.value, null)
    fail = false; await state.load(); assert.equal(state.result.value.setting_revision_id, 'setting')
    let resolve!: (value: unknown) => void
    bibleVariantApi.source = () => new Promise(done => { resolve = done })
    const pending = state.load()
    props.detail = { ...detail(), setting: { ...detail().setting, id: 'another-revision' } }
    await nextTick(); resolve(evidence()); await pending
    assert.equal(state.result.value, null); assert.equal(state.opened.value, false); assert.equal(state.loading.value, false)
  } finally { app.unmount() }
})
