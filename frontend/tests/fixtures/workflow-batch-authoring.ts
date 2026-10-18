import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'
import SettingsView from '../../src/views/SettingsView.vue'
import { useAuthStore } from '../../src/stores/auth'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import { copy, installReads } from './workflow-import-data'
import { batchScan, batchPreview, batchReceipt } from './workflow-batch-data'
import '../../src/styles.css'
import '../../src/platform.css'

const writes = ref(0), reads = ref(0), blocked = ref(0), mode = ref('success'), waiting = ref(false)
const attempts = ref<Array<{deploymentId: string; items: any[]}>>([])
let release: (() => void) | undefined
function deny(): never { blocked.value++; throw Error('隔离页面禁止真实网络、模型及主机请求') }
api.interceptors.request.use(() => deny()); api.defaults.adapter = async () => deny()
window.fetch = async () => deny(); XMLHttpRequest.prototype.send = () => deny(); navigator.sendBeacon = () => deny()
installReads(platformApi)
for (const key of Object.keys(platformApi).filter(k => k.startsWith('list') || k.startsWith('get') || k === 'resolveConfiguration')) {
  const original = (platformApi as any)[key]
  ;(platformApi as any)[key] = async (...args: any[]) => { reads.value++; return original(...args) }
}
platformApi.scanComfySavedWorkflows = async () => { reads.value++; return batchScan() as any }
platformApi.previewComfySavedWorkflow = async (id, input) => { writes.value++; return batchPreview(id, input) }
platformApi.batchImportComfySavedWorkflows = async (id, items) => {
  const selectedMode = mode.value
  writes.value++; attempts.value.push({ deploymentId: id, items: copy(items) })
  if (attempts.value.length === 2) {
    waiting.value = true
    await new Promise<void>(resolve => { release = resolve })
    waiting.value = false; release = undefined
    if (selectedMode === 'unknown') throw Error('隔离第二批响应丢失，未声明保存成功')
    if (selectedMode === 'wrong-source') {
      const result = batchReceipt(id, items); result.items[0].sourceDeploymentId = 'batch-b'; return result as any
    }
  }
  return batchReceipt(id, items, selectedMode) as any
}
api.get = async (url: string) => {
  if (url === '/registry-relations/') { reads.value++; return { data: { count: 0, next: null, results: [], generated_at: '2026-10-10T00:00:00Z', context_label: '隔离关系' } } as any }
  return deny()
}
const pinia = createPinia()
useAuthStore(pinia).user = { id: `workflow-batch-fixture-${crypto.randomUUID()}`, isStaff: true, isSuperuser: true } as any
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: SettingsView }] })
await router.push('/settings?section=workflows'); await router.isReady()
createApp({ setup: () => () => h('main', { style: 'max-width:1400px;margin:auto;padding:12px;min-width:0' }, [
  h('h1', { style: 'font-size:20px' }, '多来源与多批次 · 内存隔离验证'),
  h('p', { 'data-fixture-evidence': '', role: 'status' }, `内存写入 ${writes.value} · 批次提交 ${attempts.value.length} · 读取 ${reads.value} · 网络拦截 ${blocked.value} · 不写现用库`),
  h('label', ['第二批回执', h('select', { value: mode.value, disabled: !!attempts.value.length, onChange: (e: Event) => { mode.value = (e.target as HTMLSelectElement).value } }, ['success', 'item-failure', 'unknown', 'wrong-source'].map(value => h('option', { value }, value)))]),
  h('button', { type: 'button', disabled: !waiting.value, style: 'min-height:44px', onClick: () => release?.() }, '返回第二批回执'),
  h('details', [h('summary', '内存请求记录'), h('pre', { style: 'white-space:pre-wrap;overflow-wrap:anywhere', 'data-fixture-requests': '' }, JSON.stringify(attempts.value, null, 2))]),
  h(RouterView),
]) }).use(pinia).use(router).mount('#app')
