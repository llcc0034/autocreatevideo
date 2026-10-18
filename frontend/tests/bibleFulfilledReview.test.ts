import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'
import { createSSRApp, createRenderer, reactive, ssrContextKey, nextTick, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createRouter, createMemoryHistory } from 'vue-router'
import { canonical, assets, id, sha } from './fixtures/bible-fulfilled-review-data.ts'

test('original Bible review consumes exact fulfilled results without choosing again', async t => {
  const vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try {
    const { parseBibleImageCandidateManifest: parse } = await vite.ssrLoadModule('/src/services/productionRunReview.ts')
    const { initialBibleImageSelection: initial, reviewedBibleImageBindings: bindings } = await vite.ssrLoadModule('/src/services/bibleImageSelections.ts')
    const entity = parse(canonical(), sha).value.entities[0]
    const key = `character:${id(4)}`, selection = { [key]: initial(entity) }
    await t.test('generic uploads and registered variants preserve exact version, source and primary', () => {
      assert.equal(entity.fulfilledMaterials.length, 3)
      assert.deepEqual(entity.fulfilledMaterials.map(m => m.source), ['upload', 'generated', 'generated'])
      assert.deepEqual(selection[key], { selectedArtifactIds: [], selectedAssetIds: [id(10), id(11), id(12)], anchorArtifactId: '', anchorAssetId: id(10) })
      const body = bindings([entity], selection, assets(), {}, 8)[0]
      assert.deepEqual(body.selected_library_assets, [10, 11, 12].map(n => ({ asset_id: id(n), expected_sha256: sha })))
      assert.equal(body.anchor_library_asset_id, id(10)); assert.deepEqual(body.selected_artifact_ids, [])
    })
    await t.test('missing, duplicated, mismatched and orphaned projection fails closed', () => {
      const changes = [
        c => delete c.entities[0].fulfilled_materials,
        c => c.entities[0].fulfilled_materials.pop(),
        c => c.entities[0].fulfilled_materials.push(c.entities[0].fulfilled_materials[0]),
        c => c.entities[0].fulfilled_materials[1].reference.reference_version_id = id(999),
        c => c.entities[0].fulfilled_materials[1].appearance_role = 'primary',
        c => c.entities[0].fulfilled_materials[1].checksum = '',
        c => c.material_generations = {},
        c => c.entities[0].fulfilled_materials[1].asset_id = id(10),
      ]
      // The last change alone is legitimate (same image, several versions).
      for (const change of changes.slice(0, -1)) { const c: any = canonical(); change(c); assert.ok(parse(c, sha).error) }
      const c: any = canonical(); changes.at(-1)!(c); c.entities[0].fulfilled_materials[1].checksum = 'b'.repeat(64)
      assert.ok(parse(c, sha).error)
    })
    await t.test('a reused generated origin stays a fulfillment, not a new Run candidate', () => {
      const c: any = canonical()
      c.material_fulfillments[0].source = 'generated'
      c.entities[0].fulfilled_materials[0].reference.source = 'generated'
      const parsed = parse(c, sha)
      assert.equal(parsed.error, '')
      const e = parsed.value.entities[0], chosen = { [key]: initial(e) }
      assert.equal(e.fulfilledMaterials[0].source, 'generated')
      assert.ok(e.fulfilledMaterials[0].reference.fulfillment_id)
      assert.equal(e.fulfilledMaterials[0].reference.decision_id, undefined)
      const body = bindings([e], chosen, assets(), {}, 8)[0]
      assert.deepEqual(body.selected_artifact_ids, [])
      assert.equal(body.anchor_library_asset_id, id(10))
    })
    await t.test('lost file metadata, different hash, inactive asset, dropped variant and replacement anchor cannot submit', () => {
      for (const change of [{ checksum: 'b'.repeat(64) }, { mimeType: 'video/mp4' }, { isActive: false }, { mediaUrl: '' }]) {
        const lib = assets(); Object.assign(lib[1], change); assert.throws(() => bindings([entity], selection, lib, {}, 8))
      }
      for (const change of [{ selectedAssetIds: [id(10)] }, { anchorAssetId: id(11) }]) {
        assert.throws(() => bindings([entity], { [key]: { ...selection[key], ...change } }, assets(), {}, 8))
      }
      assert.throws(() => bindings([entity], selection, assets(), {}, 2))
      assert.throws(() => bindings([entity], selection, assets(), {}, 0))
    })
    await t.test('same image fulfilling different versions is one submitted Asset, no duplicated selection', () => {
      const c: any = canonical(); c.entities[0].fulfilled_materials[1].asset_id = id(10)
      const e = parse(c, sha).value.entities[0]
      assert.equal(e.fulfilledMaterials.length, 3)
      assert.equal(bindings([e], { [key]: initial(e) }, assets(), {}, 8)[0].selected_library_assets.length, 2)
    })
    await t.test('ordinary manual candidate manifest keeps its existing contract', () => {
      const c: any = canonical(); delete c.material_fulfillments; delete c.material_generations; delete c.entities[0].fulfilled_materials
      const e = parse(c, sha).value.entities[0]
      assert.deepEqual(initial(e), { selectedArtifactIds: [], selectedAssetIds: [], anchorArtifactId: '', anchorAssetId: '' })
      assert.throws(() => bindings([e], selection, assets(), {}, 8)) // No generic-library bypass.
    })
    await t.test('editor handlers cannot remove a fulfillment or replace its primary; refreshing retries failed previews', async () => {
      const { default: Editor } = await vite.ssrLoadModule('/src/components/BibleImageSelectionEditor.vue')
      const props = reactive({ entities: [entity], assets: assets(), history: {}, modelValue: structuredClone(selection) })
      const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
      let state: any
      const app = renderer.createApp({ setup() { state = Editor.setup(props, { expose() {}, emit(_name, value) { props.modelValue = value } }); return () => h('div') } })
      try {
        app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
        state.toggleBibleLibraryAsset('character', id(4), id(11), false)
        state.setBibleLibraryAnchor('character', id(4), id(11))
        state.setBibleAnchor('character', id(4), id(601))
        assert.deepEqual(props.modelValue, selection)
        const material = entity.fulfilledMaterials[0]
        state.failedPreviews.value.add(material.referenceVersionId)
        assert.equal(state.materialPreview(material), '')
        props.assets = assets(); await nextTick()
        assert.ok(state.materialPreview(material))
      } finally { app.unmount() }
    })
    await t.test('shared editor renders truthful provenance, fixed primary, paging and explicit missing media', async () => {
      const { default: Editor } = await vite.ssrLoadModule('/src/components/BibleImageSelectionEditor.vue')
      const e = parse(canonical(55), sha).value.entities[0]
      const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }, { path: '/assets', component: { render: () => null } }] })
      await router.push('/'); await router.isReady()
      const html = await renderToString(createSSRApp({ render: () => h(Editor, { entities: [e], assets: assets(55), history: {}, modelValue: { [key]: initial(e) } }) }).use(router))
      assert.match(html, /主图 · 已固定/); assert.match(html, /上传素材/); assert.match(html, /模型生成并登记/)
      assert.match(html, /12 \/ 55/); assert.match(html, /再显示 12 项履约素材/)
      assert.equal((html.match(/<summary[^>]*>确切版本与来源/g) || []).length, 12)
      const missing = await renderToString(createSSRApp({ render: () => h(Editor, { entities: [entity], assets: [], history: {}, modelValue: selection }) }).use(router))
      assert.match(missing, /素材预览或摘要不可用/)
    })
  } finally { await vite.close() }
})
