import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, ssrContextKey } from 'vue'
import { emptyVariantSetting, readVariantDetail, readVariantPage, readVariantSummary, requireVariantScope,
  appearanceRoleLabel, primarySettingTranslationFields, variantCreatePayload, variantDecisionLabel, variantRequestGuard, variantReviewPayload, variantRevisePayload, variantSettingDraft } from '../src/services/bibleVariants.ts'

const fixture = (): any => ({ id: 'variant-a', project_id: 'project-a', character_id: 'character-a', scene_id: null, prop_id: null,
  appearance_role: 'primary', source: null, primary_setting: null,
  name: '雨衣', reference_name: 'Hana · 雨衣', revision: 2, current_decision: null, created_by_id: 1, created_at: '2026-09-27T00:00:00Z', updated_at: '2026-09-27T01:00:00Z',
  setting: { id: 'revision-b', revision: 2, name: '雨衣', reference_name: 'Hana · 雨衣', identity_anchor: '共同身份', appearance_anchor: '本造型设定',
    basis_asset_id: null, basis_asset_sha256: '', content_sha256: 'b'.repeat(64), payload_schema_version: 2,
    source_declaration_sha256: '', primary_setting_revision_id: null,
    created_by_id: 1, created_at: '2026-09-27T01:00:00Z', review: null } })
const scope = { project_id: 'project-a', entity_kind: 'character' as const, entity_id: 'character-a' }
const page = (results: any[]) => ({ count: results.length, next: null, previous: null, results })

test('actor identifiers follow the integer API contract without string coercion', () => {
  const value = fixture()
  value.setting.review = { id: 'review-a', decision: 'approve', comment: '', decided_by_id: 7, created_at: value.created_at }
  value.current_decision = 'approve'
  assert.equal(readVariantDetail(value).setting.review?.decided_by_id, 7)
  for (const actor of ['7', 'user-a', 0, -1, 1.5, true, null, undefined]) {
    assert.throws(() => readVariantDetail({ ...value, created_by_id: actor }))
    assert.throws(() => readVariantDetail({ ...value, setting: { ...value.setting, created_by_id: actor } }))
    assert.throws(() => readVariantDetail({ ...value, setting: { ...value.setting,
      review: { ...value.setting.review, decided_by_id: actor } } }))
  }
})

test('only the viewed current primary supplies separately identified translation sources', () => {
  const detail = fixture()
  const fields = primarySettingTranslationFields(detail, scope)
  assert.deepEqual(fields.map(row => [row.fieldKey, row.sourceText]), [
    ['character.primary_setting.identity_anchor', '共同身份'], ['character.primary_setting.appearance_anchor', '本造型设定']])
  assert.ok(fields.every(row => row.scopeId === scope.entity_id && row.scopeType === 'character'))
  detail.setting.identity_anchor = ''
  assert.equal(primarySettingTranslationFields(detail, scope).length, 1)
  detail.setting.revision = 1
  assert.deepEqual(primarySettingTranslationFields(detail, scope), [])
  detail.setting.revision = 2; detail.appearance_role = 'variant'
  assert.deepEqual(primarySettingTranslationFields(detail, scope), [])
  assert.throws(() => primarySettingTranslationFields(fixture(), { ...scope, entity_id: 'other' }))
  for (const [kind, idKey] of [['scene', 'scene_id'], ['prop', 'prop_id']] as const) {
    const row = { ...fixture(), character_id: null, [idKey]: `${kind}-a` }
    assert.equal(primarySettingTranslationFields(row, { ...scope, entity_kind: kind, entity_id: row[idKey] })[1]?.fieldKey,
      `${kind}.primary_setting.appearance_anchor`)
  }
})

test('formal pages keep setting translation with its owner and invalidate dependent input state', () => {
  const editor = readFileSync(new URL('../src/components/BibleVariantEditor.vue', import.meta.url), 'utf8')
  assert.match(editor, /TranslationReviewPanel v-if="translationFields.length && !detailError" :key="detail.setting.id"/)
  assert.match(editor, /@changed="emit\('translationsChanged'\)"/)
  const series = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  assert.match(series, /@translations-changed="markProjectInputsChangedFromTranslation"/)
  for (const page of [series, readFileSync(new URL('../src/views/StoryboardView.vue', import.meta.url), 'utf8')])
    assert.doesNotMatch(page, /(?:character|scene|prop)\.visual_identity/)
})

test('draft uses exact viewed revision and leaves language, source and server facts intact', () => {
  const detail = fixture(), before = structuredClone(detail)
  const draft = variantSettingDraft(readVariantDetail(detail))
  assert.equal(draft.reference_name, 'Hana · 雨衣')
  assert.deepEqual(Object.keys(draft).sort(), ['appearance_anchor', 'basis_asset_id', 'basis_asset_sha256', 'identity_anchor', 'name', 'reference_name'])
  draft.appearance_anchor = '新的造型'
  assert.deepEqual(detail, before)
  assert.equal(emptyVariantSetting().appearance_anchor, '')
  assert.equal(emptyVariantSetting().basis_asset_id, null)
  assert.equal(variantRevisePayload(detail, draft).expected_revision, 2)
  assert.equal(variantRevisePayload(detail, draft).appearance_anchor, '新的造型')
})

test('review submits exact revision and SHA; old revisions and existing decisions cannot be overwritten', () => {
  const detail = fixture()
  assert.deepEqual(variantReviewPayload(detail, 'approve', '已核对'), { expected_revision: 2, expected_sha256: 'b'.repeat(64), decision: 'approve', comment: '已核对' })
  detail.setting.revision = 1
  assert.throws(() => variantReviewPayload(detail, 'approve', ''))
  assert.throws(() => variantRevisePayload(detail, emptyVariantSetting()))
  detail.setting.revision = 2
  detail.setting.review = { decision: 'reject' }
  assert.throws(() => variantReviewPayload(detail, 'approve', ''))
  assert.equal(variantDecisionLabel(null), '尚未人工核对')
  assert.match(variantDecisionLabel('approve'), /设定已确认/)
  assert.throws(() => variantDecisionLabel('anything' as any))
})

test('readers reject incomplete, conflicting and wrong-owner responses instead of pretending success', () => {
  const value = fixture()
  assert.deepEqual(readVariantDetail(value), value)
  assert.equal(requireVariantScope(readVariantSummary(value), scope).id, value.id)
  assert.throws(() => requireVariantScope(value, { ...scope, entity_id: 'other' }))
  assert.throws(() => requireVariantScope(value, { ...scope, project_id: 'other' }))
  for (const bad of [null, {}, { ...value, current_decision: 'dispatched' }, { ...value, scene_id: 'scene-a' },
    { ...value, revision: null }, { ...value, setting: { ...value.setting, reference_name: 'another' } },
    { ...value, setting: { ...value.setting, appearance_anchor: undefined } },
    { ...value, setting: { ...value.setting, identity_anchor: null } },
    { ...value, setting: { ...value.setting, content_sha256: '' } },
    { ...value, setting: { ...value.setting, basis_asset_id: 'asset-a', basis_asset_sha256: '' } },
    { ...value, setting: { ...value.setting, review: undefined } }]) assert.throws(() => readVariantDetail(bad))
  value.setting.revision = 1
  value.setting.reference_name = '旧引用名称'
  value.setting.review = { id: 'review-a', decision: 'approve', comment: '', decided_by_id: 1, created_at: value.created_at }
  assert.equal(readVariantDetail(value).current_decision, null, 'old approval is not current approval')
})

test('list/history stay bounded; no invented empty page and no following arbitrary next URLs', () => {
  assert.equal(readVariantPage(page([fixture()]), readVariantSummary).results.length, 1)
  for (const bad of [{}, { ...page([]), count: -1 }, { ...page([]), results: {} }, { ...page([]), next: false },
    { ...page([fixture()]), count: 0 }, page(Array.from({ length: 51 }, fixture))]) assert.throws(() => readVariantPage(bad, readVariantSummary))
})

test('declared variant uses its exact primary revision without copying creative text', () => {
  const value = fixture()
  value.appearance_role = 'variant'
  value.source = { binding_id: 'binding', script_revision_id: 'output', entity_key: 'person', variant_key: 'rain', declaration_sha256: 'a'.repeat(64) }
  value.setting.identity_anchor = ''
  value.setting.primary_setting_revision_id = 'primary-1'
  value.primary_setting = { ...fixture().setting, id: 'primary-1', variant_id: 'primary', revision: 1,
    reference_name: '主要形象', source_declaration_sha256: 'c'.repeat(64) }
  assert.equal(readVariantDetail(value).primary_setting?.revision, 1)
  assert.equal(variantSettingDraft(value).identity_anchor, '')
  for (const bad of [ { ...value, primary_setting: null }, { ...value, appearance_role: 'primary' },
    { ...value, primary_setting: { ...value.primary_setting, id: 'primary-2' } },
    { ...value, primary_setting: { ...value.primary_setting, primary_setting_revision_id: 'sibling' } },
    { ...value, source: { ...value.source, variant_key: null } },
    { ...value, setting: { ...value.setting, identity_anchor: '重复身份' } } ]) assert.throws(() => readVariantDetail(bad))
  value.setting.appearance_anchor = ''
  assert.equal(readVariantDetail(value).setting.appearance_anchor, '')
  assert.equal(appearanceRoleLabel('primary'), '主图设定')
  assert.throws(() => appearanceRoleLabel('unknown' as any))
})

test('primary declarations and original blanks are not inferred from missing response fields', () => {
  const value = fixture()
  value.appearance_role = 'primary'
  value.source = { binding_id: 'binding', script_revision_id: 'output', entity_key: 'person', variant_key: null, declaration_sha256: 'a'.repeat(64) }
  value.setting.identity_anchor = ''; value.setting.appearance_anchor = ''
  assert.equal(readVariantDetail(value).setting.identity_anchor, '')
  for (const key of ['appearance_role', 'source', 'primary_setting']) {
    const bad = structuredClone(value); delete bad[key]; assert.throws(() => readVariantDetail(bad))
  }
})

test('late list, detail and unmounted responses cannot overwrite a newer selection', () => {
  const gate = variantRequestGuard(), first = gate.next(), second = gate.next()
  assert.equal(gate.current(first), false)
  assert.equal(gate.current(second), true)
  gate.cancel()
  assert.equal(gate.current(second), false)
})

test('manual blanks remain explicit values without invented identity or image readiness', () => {
  const value = fixture()
  value.setting.identity_anchor = ''; value.setting.appearance_anchor = ''
  const detail = readVariantDetail(value)
  const draft = variantSettingDraft(detail)
  assert.equal(draft.identity_anchor, ''); assert.equal(draft.appearance_anchor, '')
  assert.equal(draft.basis_asset_id, null)
  assert.equal(variantRevisePayload(detail, draft).identity_anchor, '')
  assert.equal(detail.current_decision, null)
})

test('manual creation explicitly pins the viewed primary revision and never copies its identity', () => {
  const primary = fixture(), value = { ...emptyVariantSetting(), name: '雨衣', reference_name: '人物雨衣' }
  primary.setting.revision = 1
  const body = variantCreatePayload(value, primary)
  assert.equal(body.appearance_role, 'variant')
  assert.equal(body.primary_setting_revision_id, primary.setting.id)
  assert.equal(body.primary_setting_sha256, primary.setting.content_sha256)
  assert.equal(body.identity_anchor, '')
  assert.equal(variantCreatePayload(value, null).appearance_role, 'primary')
  assert.equal(variantCreatePayload(value, null).primary_setting_revision_id, null)
  assert.throws(() => variantCreatePayload(value, { ...primary, appearance_role: 'variant' }))
  assert.throws(() => variantCreatePayload({ ...value, identity_anchor: '重复' }, primary))
  assert.throws(() => readVariantDetail({ ...fixture(), appearance_role: 'variant' }))
})

test('editor sends explicit blank text, retains errors and preserves exact viewed revision', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  context.after(() => vite.close())
  const { bibleVariantApi: client } = await vite.ssrLoadModule('/src/services/bibleVariantApi.ts')
  const calls: any[] = []
  let fail = true
  client.list = async () => page([])
  client.create = async (_scope, payload) => {
    calls.push(structuredClone(payload))
    if (fail) throw Error('隔离保存失败')
    const result = fixture()
    Object.assign(result, { name: payload.name, reference_name: payload.reference_name })
    Object.assign(result.setting, payload)
    return result
  }
  client.revise = async (basis, payload) => { calls.push({ basis, payload }); return basis }
  const { default: Component } = await vite.ssrLoadModule('/src/components/BibleVariantEditor.vue')
  const renderer = createRenderer<any,any>({ createElement:()=>({}),createText:()=>({}),createComment:()=>({}),
    insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){} })
  let state: any
  const app = renderer.createApp({ setup() { state = Component.setup({ scope, entityName: '隔离人物' }, { emit(){}, expose(){} }); return () => h('div') } })
  try {
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await nextTick()
    state.startCreate(); state.draft.value.name = '待补充'; state.draft.value.reference_name = '人物·待补充'
    await state.save()
    assert.match(state.mutationError.value, /隔离保存失败/)
    assert.equal(state.editing.value, true); assert.equal(state.draft.value.name, '待补充')
    assert.deepEqual(calls[0], { ...emptyVariantSetting(), name:'待补充', reference_name:'人物·待补充' })
    fail = false; await state.save()
    assert.equal(state.editing.value, false); assert.equal(state.detail.value.setting.identity_anchor, '')
    state.startEdit(); state.draft.value.appearance_anchor = '补充外观'; await state.save()
    assert.equal(calls[2].basis.setting.revision, 2)
    assert.equal(calls[2].payload.identity_anchor, '')
    assert.equal(calls[2].payload.appearance_anchor, '补充外观')
    const selected = JSON.parse(JSON.stringify(state.detail.value))
    state.startCreate(selected)
    assert.equal(state.creationPrimary.value.setting.id, selected.setting.id)
    assert.equal(state.draft.value.identity_anchor, '')
    assert.equal(state.primaryBasis.value.id, selected.setting.id)
    state.reset()
    assert.equal(state.creationPrimary.value, null)
  } finally { app.unmount() }
})

test('formal API client uses only scoped reads and explicit setting mutations, never production calls', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true } })
  context.after(() => vite.close())
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const calls: any[] = []
  api.get = async (url: string, config: any) => { calls.push({ method: 'get', url, config }); return { data: url === '/bible-variants/' ? page([fixture()]) : fixture() } }
  api.post = async (url: string, body: any) => { calls.push({ method: 'post', url, body }); return { data: fixture() } }
  const { bibleVariantApi: client } = await vite.ssrLoadModule('/src/services/bibleVariantApi.ts')
  await client.list(scope, 2)
  assert.deepEqual(calls[0].config.params, { ...scope, page: 2 })
  await client.detail('variant-a')
  await client.revise(fixture(), variantSettingDraft(fixture()))
  await client.review(fixture(), 'reject', '需修改')
  assert.equal(calls[2].body.expected_revision, 2)
  assert.equal(calls[3].body.expected_sha256, 'b'.repeat(64))
  assert.equal(calls[3].body.decision, 'reject')
  await client.create(scope, emptyVariantSetting(), null)
  assert.equal(calls[4].body.appearance_role, 'primary')
  assert.equal(calls[4].body.primary_setting_revision_id, null)
  await assert.rejects(client.create({ ...scope, entity_id:'different' }, emptyVariantSetting(), fixture()))
  assert.equal(calls.length, 5)
  assert.ok(calls.every(call => call.url.startsWith('/bible-variants/')))
  api.get = async () => ({ data: { ...fixture(), id: 'another' } })
  await assert.rejects(client.detail('variant-a'))
  api.get = async () => ({ data: fixture() })
  await assert.rejects(client.detail('variant-a', 1))
})

test('one Bible editor entrance, no asset-library editor or hidden-tab eager loading', () => {
  const component = readFileSync(new URL('../src/components/BibleVariantEditor.vue', import.meta.url), 'utf8')
  const view = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  const library = readFileSync(new URL('../src/views/AssetLibraryView.vue', import.meta.url), 'utf8')
  assert.match(view, /v-if="variantEntity"/)
  assert.match(view, /管理主图与变体/)
  assert.match(view, /:dismissible="!variantBusy"/)
  assert.match(component, /已核对本修订/)
  assert.match(component, /:disabled="saving \|\| !accepted"/)
  assert.match(component, /主图采用 → 参考主图生成变体的生产链尚未接入/)
  assert.match(component, /onBeforeUnmount/)
  assert.doesNotMatch(component, /identityRequired|appearanceRequired|<textarea[^>]*required/)
  assert.match(component, /保存不代表图片已就绪/)
  assert.match(component, /不随主图的新修订自动变化/)
  assert.doesNotMatch(component, /v-html|\/production-runs|generate|dispatch|\.delete\(/)
  assert.doesNotMatch(library, /BibleVariantEditor/)
})

test('setting sections and human review status do not confuse generated candidates with wardrobe variants', () => {
  const component = readFileSync(new URL('../src/components/BibleVariantEditor.vue', import.meta.url), 'utf8')
  const view = readFileSync(new URL('../src/views/SeriesStudioView.vue', import.meta.url), 'utf8')
  for (const title of ['共同身份', '主图外观', '变体差异', '保存影响']) assert.ok(component.includes(`aria-label="${title}"`))
  assert.match(component, /没有人审记录不等于生产流程未通过/)
  assert.match(component, /已有变体仍引用原来的确切主图修订/)
  assert.match(component, /\.setting-text\{white-space:pre-wrap\}/)
  assert.match(view, /生成候选序号 \{\{ candidate\.variantIndex \}\}/)
  assert.doesNotMatch(view, /变体 \{\{ candidate\.variantIndex/)
})

test('primary and two variants edit independently and retain exact primary and historical revisions', async context => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  context.after(() => vite.close())
  const { bibleVariantApi: client } = await vite.ssrLoadModule('/src/services/bibleVariantApi.ts')
  const primary = fixture()
  primary.id = 'primary'; primary.setting.identity_anchor = '共同身份\n' + '长描述。'.repeat(120)
  const first = fixture(), second = fixture()
  for (const [index, value] of [first, second].entries()) {
    value.id = `variant-${index + 1}`; value.appearance_role = 'variant'
    value.setting.id = `revision-${index + 1}`; value.setting.identity_anchor = ''
    value.setting.appearance_anchor = `差异 ${index + 1}`
    value.setting.primary_setting_revision_id = primary.setting.id
    value.primary_setting = { ...structuredClone(primary.setting), variant_id: primary.id }
  }
  const records = new Map([primary, first, second].map(row => [row.id, [structuredClone(row)]]))
  client.list = async () => page([...records.values()].map(versions => structuredClone(versions.at(-1))))
  client.detail = async (id, revision) => {
    const versions = records.get(id)!, current = versions.at(-1)!
    const selected = revision ? versions.find(row => row.setting.revision === revision)! : current
    return { ...structuredClone(current), setting: structuredClone(selected.setting), primary_setting: structuredClone(selected.primary_setting) }
  }
  const writes: any[] = []
  client.revise = async (basis, payload) => {
    const current = records.get(basis.id)!.at(-1)!
    assert.equal(basis.setting.revision, current.revision)
    writes.push({ id: basis.id, payload: structuredClone(payload) })
    const next = { ...structuredClone(current), name: payload.name, reference_name: payload.reference_name,
      revision: current.revision + 1, setting: { ...current.setting, ...payload, revision: current.revision + 1,
        id: `${basis.id}-revision-${current.revision + 1}`, review: null } }
    records.get(basis.id)!.push(next); return structuredClone(next)
  }
  const { default: Component } = await vite.ssrLoadModule('/src/components/BibleVariantEditor.vue')
  const renderer = createRenderer<any,any>({ createElement:()=>({}),createText:()=>({}),createComment:()=>({}),
    insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null,patchProp(){} })
  let state: any
  const app = renderer.createApp({ setup() { state = Component.setup({ scope, entityName: '隔离人物' }, { emit(){}, expose(){} }); return () => h('div') } })
  try {
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); await nextTick()
    await state.openDetail(primary.id); state.startEdit(); state.draft.value.identity_anchor = '新身份'; await state.save()
    assert.equal(records.get(first.id)!.length, 1); assert.equal(records.get(second.id)!.length, 1)
    for (const value of [first, second]) {
      await state.openDetail(value.id); state.startEdit()
      assert.equal(state.primaryBasis.value.identity_anchor, primary.setting.identity_anchor)
      assert.equal(state.draft.value.identity_anchor, '')
      state.draft.value.appearance_anchor += '\n单独补充'; await state.save()
    }
    assert.deepEqual(writes.map(row => row.id), [primary.id, first.id, second.id])
    assert.equal(records.get(primary.id)!.at(-1)!.setting.identity_anchor, '新身份')
    for (const value of [first, second]) {
      const current = records.get(value.id)!.at(-1)!
      assert.equal(current.setting.appearance_anchor, value.setting.appearance_anchor + '\n单独补充')
      assert.deepEqual(current.primary_setting, value.primary_setting)
    }
    await state.openDetail(first.id, 2)
    assert.equal(state.isCurrent.value, false); assert.equal(state.canReview.value, false)
    state.startEdit(); assert.equal(state.editing.value, false)
    assert.equal(state.detail.value.setting.appearance_anchor, '差异 1')
  } finally { app.unmount() }
})
