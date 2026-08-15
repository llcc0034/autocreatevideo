import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('asset management sends category corrections and exposes guarded deletion', () => {
  const view = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  const repository = readFileSync(new URL('../src/services/repository.ts', import.meta.url), 'utf8')
  const store = readFileSync(new URL('../src/stores/studio.ts', import.meta.url), 'utf8')

  assert.ok(repository.includes('kind: payload.kind, style:'))
  assert.ok(repository.includes('await api.delete(`/assets/${assetId}/`)'))
  assert.ok(store.includes('await repository.deleteAsset(assetId)'))
  assert.ok(store.includes('assets.value=assets.value.filter((asset)=>asset.id!==assetId)'))
  assert.ok(view.includes('<select v-model="assetForm.kind" :disabled="assetSaving">'))
  assert.ok(view.includes('编辑素材信息'))
  assert.ok(view.includes('删除素材'))
  assert.ok(view.includes('v-model="deleteConfirmName"'))
  assert.ok(view.includes(':disabled="deletePending||!deleteConfirmed"'))
  assert.ok(view.includes('role="alert"'))
})
