import { createApp, h, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import BibleVariantEditor from '../../src/components/BibleVariantEditor.vue'
import { bibleVariantApi } from '../../src/services/bibleVariantApi'
import { api } from '../../src/services/api'
import { repository } from '../../src/services/repository'
import type { BibleVariantDetail, BibleVariantSettingInput } from '../../src/services/bibleVariants'
import '../../src/styles.css'

// UI-only in-memory fixture. Real request interception stays active even if a
// component accidentally starts calling an endpoint outside these test stubs.
api.interceptors.request.use(() => { throw Error('隔离样例禁止真实API请求') })
const scope = { project_id: 'isolated-project', entity_kind: 'character' as const, entity_id: 'isolated-character' }
const history: BibleVariantDetail[] = []
const failNext = ref(false), writes = ref(0)
const failSource = ref(false)
repository.getProjectTranslations = async () => []
repository.createManualProjectTranslation = async () => { throw Error('隔离验证：手工候选保存失败，未写入业务数据。') }
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
const paging = <T>(results: T[]) => ({ count: results.length, next: null, previous: null, results: clone(results) })
function save(value: BibleVariantSettingInput, previous?: BibleVariantDetail, primary?: BibleVariantDetail | null): BibleVariantDetail {
  if (failNext.value) { failNext.value = false; throw Error('隔离验证：保存失败，未写入样例。') }
  const revision = previous ? previous.revision + 1 : 1
  const stamp = '2026-10-01T01:00:00Z'
  const dependency = previous?.primary_setting ?? (primary ? { ...clone(primary.setting), variant_id: primary.id } : null)
  const row: BibleVariantDetail = { id: previous?.id ?? `isolated-appearance-${writes.value + 1}`, project_id: scope.project_id, character_id: scope.entity_id,
    scene_id: null, prop_id: null, appearance_role: previous?.appearance_role ?? (primary ? 'variant' : 'primary'), source: previous?.source ? clone(previous.source) : null, primary_setting: dependency,
    name: value.name, reference_name: value.reference_name, revision, current_decision: null,
    created_by_id: 1, created_at: stamp, updated_at: stamp,
    setting: { ...clone(value), id: `isolated-revision-${writes.value + 1}`, revision, content_sha256: 'a'.repeat(64),
      payload_schema_version: 2, source_declaration_sha256: '', primary_setting_revision_id: dependency?.id ?? null,
      created_by_id: 1, created_at: stamp, review: null } }
  history.push(row); writes.value++; return clone(row)
}
bibleVariantApi.list = async () => paging([...new Map(history.map(row => [row.id, row])).values()])
bibleVariantApi.detail = async (id, revision) => {
  const versions = history.filter(row => row.id === id)
  const current = versions.at(-1), selected = revision ? versions.find(row => row.revision === revision) : current
  if (!current || !selected) throw Error('隔离样例修订不存在')
  return { ...clone(current), setting: clone(selected.setting), primary_setting: clone(selected.primary_setting) }
}
bibleVariantApi.create = async (_scope, value, primary) => save(value, undefined, primary)
bibleVariantApi.revise = async (detail, value) => save(value, detail)
bibleVariantApi.history = async id => paging(history.filter(row => row.id === id).map(row => row.setting).reverse())
bibleVariantApi.review = async () => { throw Error('本隔离样例不执行审核') }
bibleVariantApi.source = async detail => {
  if (failSource.value) { failSource.value = false; throw Error('隔离验证：初始来源缺失，不能用当前剧本替代。') }
  const initial = history.find(row => row.id === detail.id)!
  const keys = ['name', 'reference_name', ...(detail.appearance_role === 'primary' ? ['identity_anchor'] : []), 'appearance_anchor'] as const
  return { appearance_id: detail.id, setting_revision_id: detail.setting.id, setting_sha256: detail.setting.content_sha256,
    source: detail.source, document_sha256: detail.source ? 'c'.repeat(64) : null,
    fields: keys.map(key => ({ key, label: { name: '设定名称', reference_name: '素材引用名称', identity_anchor: '共同身份', appearance_anchor: '主图外观 / 变体差异' }[key], declaration_value: detail.source ? initial.setting[key] : null,
      setting_value: detail.setting[key], origin: detail.source ? (key === 'reference_name' ? 'configured_literal' : key === 'appearance_anchor' ? 'configured_default' : 'source_value') : 'manual',
      source_path: detail.source && key !== 'reference_name' ? `source.cast[0].${key}` : null,
      changed_from_declaration: detail.source ? detail.setting[key] !== initial.setting[key] : null })) }
}
function seedSource() {
  seedTranslation()
  for (const row of history.filter(row => row.appearance_role === 'primary')) {
    row.source = { binding_id: 'isolated-binding', script_revision_id: 'isolated-script-revision', entity_key: 'person',
      variant_key: null, declaration_sha256: 'd'.repeat(64) }
    if (row.setting.revision === 1) row.setting.source_declaration_sha256 = 'e'.repeat(64)
  }
}
function seedTranslation() {
  if (history.length) return
  const primary = save({ name: '主要形象', reference_name: '人物·主要形象', identity_anchor: '共同身份：短发调查员',
    appearance_anchor: '主图外观：灰色雨衣', basis_asset_id: null, basis_asset_sha256: '' })
  const current = save({ ...primary.setting, appearance_anchor: '主图外观：深灰色雨衣' }, primary)
  save({ name: '夜班', reference_name: '人物·夜班', identity_anchor: '', appearance_anchor: '变体差异：戴眼镜',
    basis_asset_id: null, basis_asset_sha256: '' }, undefined, current)
}
function seedSections() {
  seedSource()
  const primary = history.filter(row => row.appearance_role === 'primary').at(-1)!
  save({ ...primary.setting, identity_anchor: '共同身份：短发调查员。\n' + '稳定身份与表情细节；不随服装变化。'.repeat(16) }, primary)
  save({ name: '外勤', reference_name: '人物·外勤', identity_anchor: '', appearance_anchor: '变体差异：棕色工作服。\n保持共同身份不变。',
    basis_asset_id: null, basis_asset_sha256: '' }, undefined, primary)
}
createApp({ setup() { return () => h('main', { style: 'max-width:900px;margin:16px auto;padding:16px;min-width:0' }, [
  h('h1', '设定空值编辑 · 隔离样例'), h('p', '真实编辑组件；仅内存模拟响应，不访问业务数据库、不调用模型。'),
  h('button', { class: 'button ghost', onClick: () => { failNext.value = true } }, '下次保存模拟失败'),
  h('button', { class: 'button ghost', disabled: writes.value > 0, onClick: seedTranslation }, '装入主图翻译样例（随后刷新清单）'),
  h('button', { class: 'button ghost', disabled: writes.value > 0, onClick: seedSource }, '装入来源对照样例（随后刷新清单）'),
  h('button', { class: 'button ghost', disabled: writes.value > 0, onClick: seedSections }, '装入主图与两个变体（随后刷新清单）'),
  h('button', { class: 'button ghost', onClick: () => { failSource.value = true } }, '下次来源读取模拟失败'),
  h('p', { role: 'status' }, `样例写入 ${writes.value} 次；${failNext.value ? '下次失败' : '正常响应'}`),
  h(BibleVariantEditor, { scope, entityName: '隔离人物' }),
]) } }).use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })).mount('#app')
