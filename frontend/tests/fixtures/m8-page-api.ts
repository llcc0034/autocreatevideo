// Opt-in full parent with REAL isolated Django APIs. Never reads saved login.
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import ProductionRunView from '../../src/views/ProductionRunView.vue'
import { api } from '../../src/services/api'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

const port = new URLSearchParams(location.search).get('bridge') || ''
if (!/^\d{4,5}$/.test(port) || Number(port) > 65535 || [5173, 8000, 8188, 5311].includes(Number(port))) throw Error('Explicit temporary bridge port required')
const origin = `http://127.0.0.1:${port}`
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
  getItem: () => null, setItem: () => { throw Error('No persistent session in test') }, removeItem: () => {},
} })
const nativeFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href)
  if (url.origin !== origin) throw Error('Blocked non-fixture fetch')
  return nativeFetch(input, { ...init, credentials: 'omit' })
}
api.defaults.baseURL = `${origin}/api/v1`
api.interceptors.request.use(config => {
  if (config.baseURL !== `${origin}/api/v1` || !String(config.url).startsWith('/') || String(config.url).startsWith('//')) throw Error('Blocked non-fixture API')
  config.withCredentials = false; return config
})
const state = ref<any>(null), error = ref(''), busy = ref(false)
async function control(action = 'state') {
  busy.value = true; error.value = ''
  try {
    const response = await fetch(`${origin}/__fixture__/${action}`, action === 'worker' ? { method: 'POST' } : undefined)
    const data = await response.json()
    if (!response.ok) throw Error(data.detail)
    state.value = data; return data
  } catch (cause) { error.value = String(cause); return null }
  finally { busy.value = false }
}
const first = await control()
if (!first) throw Error('Isolated bridge not ready')
const empty = { render: () => h('p', '本隔离组合只验证原生产父页。') }
const router = createRouter({ history: createMemoryHistory(), routes: [
  { path: '/projects/:projectId/production/:runId', name: 'production-run-detail', component: ProductionRunView },
  { path: '/projects/:projectId/production', name: 'production-run', component: empty },
  { path: '/projects/:projectId', name: 'project', component: empty },
  { path: '/workflows', name: 'workflows', component: empty },
] })
await router.push(`/projects/${first.project_id}/production/${first.run_id}`); await router.isReady()
createApp({ setup: () => () => h('main', [
  h('style', baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g, '')),
  h('section', { class: 'platform-card', style: 'margin:12px;padding:16px' }, [
    h('h1', 'M8-07 完整父页 / 真实隔离 API'),
    h('p', '四份新候选由原 M7 worker 归档，模型传输使用合成回复；一集缺文件失败。下方使用原生产页重试、真实 FFmpeg 和签名交付，不访问现用业务或模型。'),
    h('button', { class: 'button ghost', disabled: busy.value, onClick: () => control() }, '读取隔离状态'),
    h('button', { class: 'button ghost', disabled: busy.value, onClick: () => control('worker') }, '执行隔离装配队列'),
    h('button', { class: 'button ghost', disabled: busy.value, onClick: () => control('check') }, '核对完整交付证据'),
    error.value ? h('p', { role: 'alert' }, error.value) : null,
    h('p', { role: 'status' }, `隔离调用记录 ${state.value?.attempt_count}；装配记录 ${state.value?.jobs?.length}；封存 ${state.value?.bundles?.length}`),
    h('details', [h('summary', '真实隔离 API / 交付证据'), h('pre', { style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, JSON.stringify(state.value, null, 2))]),
  ]), h(RouterView),
]) }).use(createPinia()).use(router).mount('#app')
