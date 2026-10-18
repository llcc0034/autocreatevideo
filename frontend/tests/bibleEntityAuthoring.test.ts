import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

test('entity create and update persist metadata without owning setting anchors', async context => {
  const server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), logLevel: 'silent',
    appType: 'custom', server: { middlewareMode: true, hmr: false } })
  context.after(() => server.close())
  const { adaptBibleItem, persistBibleEntities } = await server.ssrLoadModule('/src/services/backendAdapters.ts')
  const { api } = await server.ssrLoadModule('/src/services/api.ts')
  const calls: any[] = [], records = new Map<string, any[]>()
  const old = { get: api.get, post: api.post, patch: api.patch, delete: api.delete }
  api.get = async (url: string) => ({ data: { results: records.get(url) || [], next: null } })
  api.post = async (url: string, body: any) => {
    calls.push({ url, body })
    const row = { ...body, id: `10000000-0000-4000-8000-00000000000${calls.length}` }
    records.set(url, [row]); return { data: row }
  }
  api.patch = async (url: string, body: any) => {
    calls.push({ url, body }); return { data: { ...body, id: url.split('/')[2] } }
  }
  api.delete = async () => { throw Error('entity save must not delete retained records') }
  const project = { id: '10000000-0000-4000-8000-000000000010', bible: Object.fromEntries(
    ['characters', 'scenes', 'props'].map(kind => [kind, [{ id: `new-${kind}`, name: `中文${kind}`,
      description: '', locked: false, referenceAssetIds: [] }]])) }
  try {
    await persistBibleEntities(project)
    assert.equal(calls.length, 3)
    for (const items of Object.values(project.bible) as any[][]) {
      assert.ok(items[0].id.startsWith('10000000-'))
      assert.equal('visualPrompt' in items[0], false)
      items[0].description = 'Only an entity note'
    }
    await persistBibleEntities(project)
    assert.equal(calls.length, 6)
    for (const { body } of calls) {
      for (const key of ['visual_identity', 'identity_anchor', 'appearance_anchor', 'visualPrompt']) assert.equal(key in body, false, key)
      assert.deepEqual(body.reference_assets, [])
      assert.equal(body.anchor_asset, null)
    }
    assert.equal(calls[0].body.stable_speaker_id, 'S1')
    assert.equal(calls[3].body.description, 'Only an entity note')
    assert.equal('visualPrompt' in adaptBibleItem({ id: 'test', name: '中文', description: '' }), false)
    api.patch = async () => { throw Error('write failed') }
    await assert.rejects(persistBibleEntities(project), /write failed/)
  } finally { Object.assign(api, old) }
})

test('studio entity form and writing guide expose one setting-text owner', () => {
  const view = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const guide = readFileSync(new URL('../src/components/PromptWritingGuide.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(view, /visualPrompt|visual_identity|缺视觉锚点|视觉锚点英文候选/)
  assert.match(view, /共同身份和外观只在“主图 \/ 变体设定”中维护/)
  assert.match(view, /无需先有图片/)
  const prefill = view.slice(view.indexOf('const selectBibleDraftAsset='), view.indexOf('</script>'))
  assert.doesNotMatch(prefill, /promptHint/)
  assert.match(prefill, /description:asset.description/)
  assert.doesNotMatch(guide, /visualPrompt/)
  assert.match(guide, /共同身份：/)
  assert.match(guide, /主图外观：/)
  assert.match(guide, /引用确切主图修订/)
})
