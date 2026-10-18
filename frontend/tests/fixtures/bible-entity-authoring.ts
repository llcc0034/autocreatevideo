import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import SeriesStudioView from '../../src/views/SeriesStudioView.vue'
import { useStudioStore } from '../../src/stores/studio'
import { api } from '../../src/services/api'
import { repository } from '../../src/services/repository'
import { platformApi } from '../../src/services/platformApi'
import { bibleVariantApi } from '../../src/services/bibleVariantApi'
import type { SeriesProject } from '../../src/types'
import '../../src/styles.css'

// Only this explicitly labelled fixture uses memory. No business API is allowed.
const blocked = ref(0), writes = ref(0), failNext = ref(false), notice = ref('')
api.interceptors.request.use(() => { blocked.value++; throw Error('隔离样例禁止真实API请求') })
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
let saved: SeriesProject = {
  id: '10000000-0000-4000-8000-000000000001', title: '实体登记隔离样例', logline: '', masterScript: '',
  isFavorite: false, styleId: 'isolated', status: 'writing', language: 'Chinese', aspectRatio: '9:16',
  resolution: '720p', width: 768, height: 1344, translationGlossary: {}, episodes: [],
  bible: { characters: [], scenes: [], props: [], negativePrompt: '', colorScript: '' },
  createdAt: '2026-10-01T00:00:00Z', updatedAt: '2026-10-01T00:00:00Z',
}
const pinia = createPinia(), studio = useStudioStore(pinia)
studio.loadStyles = async () => []
studio.loadProject = async () => clone(saved)
studio.refreshAssets = async () => []
studio.notify = (message: string) => { notice.value = message }
studio.saveProject = async project => {
  if (failNext.value) { failNext.value = false; throw Error('隔离验证：实体保存失败，未写入样例。') }
  const result = clone(project)
  for (const section of ['characters', 'scenes', 'props'] as const) for (const item of result.bible[section]) {
    if (!item.id.startsWith('10000000-')) item.id = `10000000-0000-4000-8000-${String(writes.value + 2).padStart(12, '0')}`
    if ('visualPrompt' in item) throw Error('旧字段不得写入')
  }
  saved = result; writes.value++; notice.value = '隔离实体已保存'; return clone(saved)
}
repository.getAssets = async () => []
repository.getProjectTranslations = async () => []
repository.getBibleImageCandidates = async () => ({ projectId: saved.id, candidates: [],
  counts: { characters: 0, scenes: 0, props: 0, total: 0, withCandidate: 0 } })
repository.getProductionCapability = async () => { throw Error('隔离样例未配置生产能力；本轮仅验证实体登记，不验证参考图生成。') }
platformApi.listProductionRuns = async () => []
bibleVariantApi.list = async () => ({ count: 0, next: null, previous: null, results: [] })
const router = createRouter({ history: createMemoryHistory(), routes: [
  { path: '/projects/:projectId', component: SeriesStudioView },
  { path: '/:pathMatch(.*)*', component: { render: () => h('p', '本轮仅验证实体登记') } },
] })
await router.push(`/projects/${saved.id}?tab=bible`)
await router.isReady()
createApp({ setup() { return () => h('main', { style: 'min-width:0;padding:12px' }, [
  h('aside', { style: 'padding:12px;border:1px solid #dfac68;margin-bottom:12px' }, [
    h('strong', '真实工作室组件 · 内存隔离样例'),
    h('p', '只测试实体登记和设定入口；无真实API、数据库、模型调用。参考图生产能力刻意不可用。'),
    h('button', { onClick: () => { failNext.value = true } }, '样例：下次保存失败'),
    h('p', { role: 'status' }, `样例写入 ${writes.value} 次；拦截 ${blocked.value} 次；${notice.value}`),
  ]), h(RouterView),
]) } }).use(pinia).use(router).mount('#app')
