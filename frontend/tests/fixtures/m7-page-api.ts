// Opt-in full parent page -> REAL isolated Django APIs -> original worker.
// Only remote protocol replies are synthetic. Never use this as a business entry.
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import ProductionRunView from '../../src/views/ProductionRunView.vue'
import WorkflowView from '../../src/views/WorkflowView.vue'
import { api } from '../../src/services/api'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

const port = new URLSearchParams(location.search).get('bridge') || ''
if (!/^\d{4,5}$/.test(port) || Number(port) > 65535 || [5173, 8000, 8188].includes(Number(port))) throw Error('Explicit temporary bridge port required')
const origin = `http://127.0.0.1:${port}`
// Do not read or write the user's saved login, and do not call the live backend.
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
  getItem: () => null, setItem: () => { throw Error('No persistent session in test') }, removeItem: () => {},
} })
const nativeFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href)
  if (url.origin !== origin) throw Error(`Blocked non-fixture fetch ${url.pathname}`)
  return nativeFetch(input, { ...init, credentials: 'omit' })
}
api.defaults.baseURL = `${origin}/api/v1`
api.interceptors.request.use(config => {
  if (config.baseURL !== `${origin}/api/v1` || !String(config.url).startsWith('/') || String(config.url).startsWith('//')) throw Error('Blocked non-fixture API')
  config.withCredentials = false
  return config
})
const state = ref<any>(null), error = ref(''), busy = ref(false), loseReceipt = ref(false)
api.interceptors.response.use(response => {
  if (loseReceipt.value && response.config.method === 'post' && /\/(manual-actions|retry)\/$/.test(response.config.url || '')) {
    loseReceipt.value = false
    throw Error('隔离网络注入：后端已记录命令，但此次回执丢失。')
  }
  return response
})
async function control(action = 'state', mode?: string) {
  busy.value = true; error.value = ''
  try {
    const response = await fetch(`${origin}/__fixture__/${action}`, mode ? {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }),
    } : undefined)
    const data = await response.json()
    if (!response.ok) throw Error(data.detail)
    state.value = data
    return data
  } catch (cause) { error.value = String(cause); throw cause }
  finally { busy.value = false }
}
const first = await control()
const assetCorrection = first.scenario === 'asset-correction'
const workerModes = assetCorrection ? [] : first.scenario === 'stage-retry' ? ['success', 'failure'] : ['success', 'unknown', 'cancelled', 'late']
const empty = { render: () => h('p', '仅限本轮完整生产页；其他入口未纳入这个隔离场景。') }
const router = createRouter({ history: createMemoryHistory(), routes: [
  { path: '/projects/:projectId/production/:runId', name: 'production-run-detail', component: ProductionRunView },
  { path: '/projects/:projectId/production', name: 'production-run', component: empty },
  { path: '/projects/:projectId', name: 'project', component: empty },
  { path: '/workflows', name: 'workflows', component: WorkflowView },
] })
await router.push(assetCorrection ? { name: 'workflows', query: { category: 'asset', source_id: first.job_id } }
  : `/projects/${first.project_id}/production/${first.run_id}`)
await router.isReady()
const Root = { setup: () => () => h('main', [
  h('style', baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g, '')),
  h('section', { class: 'platform-card', style: 'margin:12px;padding:16px' }, [
    h('h1', '隔离 M7 页面/API 联动 · 非现用业务'),
    h('p', assetCorrection ? '下方是原任务中心完整父页与真实 API/内存库。种子失败来自合成回包；仅允许此独立任务的人工补正，不调用模型、不采用。'
      : '下方是完整生产父页与真实 API/SQLite/worker。原始失败由合成协议回执创建；仅测试控制按钮可运行合成 worker，不访问模型。'),
    h('button', { class: 'button ghost', disabled: busy.value, onClick: () => control() }, '读取隔离调用计数'),
    assetCorrection ? null : h('label', [h('input', { type: 'checkbox', checked: loseReceipt.value, onChange: (e: Event) => { loseReceipt.value = (e.target as HTMLInputElement).checked } }), '丢弃下一次已记录的重试命令回执（隔离网络注入）']),
    ...workerModes.map(mode => h('button', {
      class: 'button ghost', disabled: busy.value, onClick: () => control('worker', mode),
    }, `合成 worker：${mode}`)),
    error.value ? h('p', { role: 'alert' }, error.value) : null,
    h('p', { role: 'status' }, assetCorrection
      ? `隔离素材任务 ${state.value?.counts?.AssetGenerationJob ?? '?'}；种子后模型调用 ${state.value?.model_invocations_after_seed ?? '?'}；人工补正 ${state.value?.confirmed_at ? 1 : 0}；采用 ${state.value?.counts?.ArtifactSelectionDecision ?? '?'}`
      : `隔离调用记录 ${state.value?.attempts?.length ?? '?'}；种子之后协议调用 ${state.value?.worker_invocations_after_seed?.reduce((n: number, c: any) => n + c.invoke_count, 0) ?? 0}；审核/采用 ${state.value?.review_or_adoption_count ?? '?'}`),
    h('details', [h('summary', '真实隔离 API/调用证据'), h('pre', { style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, JSON.stringify(state.value, null, 2))]),
  ]), h(RouterView),
]) }
createApp(Root).use(createPinia()).use(router).mount('#app')
