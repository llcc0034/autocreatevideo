import { createApp, computed, h, ref } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import Editor from '../../src/components/BibleImageSelectionEditor.vue'
import { parseBibleImageCandidateManifest } from '../../src/services/productionRunReview'
import { initialBibleImageSelection, reviewedBibleImageBindings } from '../../src/services/bibleImageSelections'
import { api } from '../../src/services/api'
import { canonical, assets, sha } from './bible-fulfilled-review-data'
import '../../src/styles.css'
import '../../src/platform.css'
api.interceptors.request.use(() => { throw Error('隔离夹具禁止业务 API') })
const count = ref(3), missing = ref(false), narrow = ref(false), receipt = ref(''), posts = ref(0)
const entities = computed(() => parseBibleImageCandidateManifest(canonical(count.value), sha).value!.entities)
const selection = ref<any>({})
const reset = (n: number) => { count.value = n; selection.value = Object.fromEntries(entities.value.map(e => [`${e.entityType}:${e.entityId}`, initialBibleImageSelection(e)])); receipt.value = '' }
reset(3)
const library = computed(() => missing.value ? [] : assets(count.value))
const error = computed(() => { try { reviewedBibleImageBindings(entities.value, selection.value, library.value, {}, 100); return '' } catch (e) { return String(e) } })
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }, { path: '/assets', component: { render: () => null } }] })
createApp({ setup() { return () => h('main', { style: { maxWidth: narrow.value ? '390px' : '1140px', margin: '20px auto', padding: '12px', boxSizing: 'border-box' } }, [
  h('h1', '原审核编辑器 · 已履约素材'), h('p', '隔离夹具：虚构图片与记录，只提交到内存，不调用真实模型、数据库或业务 API。'),
  h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '14px' } }, [
    h('button', { onClick: () => reset(3) }, '三项混合素材'), h('button', { onClick: () => reset(55) }, '55 项履约素材'),
    h('button', { onClick: () => missing.value = !missing.value }, missing.value ? '恢复素材' : '模拟素材读取缺失'),
    h('button', { onClick: () => narrow.value = !narrow.value }, narrow.value ? '桌面布局' : '390px 窄屏布局'),
  ]),
  h(Editor, { key: count.value, entities: entities.value, assets: library.value, history: {}, modelValue: selection.value, 'onUpdate:modelValue': v => selection.value = v }),
  error.value ? h('p', { role: 'alert' }, error.value) : null,
  h('button', { disabled: !!error.value, onClick: () => { const body = reviewedBibleImageBindings(entities.value, selection.value, library.value, {}, 100); posts.value++; receipt.value = `内存提交 ${posts.value} 次：主图 ${body[0]!.anchor_library_asset_id}，素材 ${body[0]!.selected_library_assets.length} 张，生成候选 ${body[0]!.selected_artifact_ids.length} 个。真实 API 0 次。` } }, '提交隔离审核'),
  h('output', { style: { display: 'block', overflowWrap: 'anywhere' } }, receipt.value),
]) } }).use(router).mount('#app')
