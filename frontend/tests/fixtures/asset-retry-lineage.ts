// Synthetic task lineage for component verification. No database/model operations.
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import Inspector from '../../src/components/TaskMediaInspector.vue'
import { api } from '../../src/services/api'
import type { AssetGenerationJob } from '../../src/types'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

export function retryFixtureJob(id: string): AssetGenerationJob {
  return { id, retryOf: id === 'original' ? null : 'original',
    retries: id === 'original' ? [{ id: 'successor', status: 'failed' }] : [],
    status: 'failed', jobType: 'asset', mediaType: 'image', imageCategory: 'reference',
    name: id === 'original' ? '合成原失败任务' : '合成失败后继', description: '',
    executionInstanceName: '隔离执行实例', executionVersion: 1, executionKind: 'deployment',
    modelDeploymentName: '隔离资源', operation: 'image.text_to_image', isExternal: false,
    allowExternalCalls: false, estimatedCost: null, actualCost: null, costCurrency: '',
    resultText: '', remoteStatus: '', errorCode: 'fixture_failure', errorMessage: '仅供 UI 验证，不是真实执行失败。',
    outputAsset: null, pollCount: 0, createdAt: '2026-10-04T00:00:00Z', finishedAt: '',
    preparation: { status: 'failed', revision: 1, reason: '合成准备记录' },
  } as AssetGenerationJob
}
export function retryInspector(job: AssetGenerationJob, capture: (state: any) => void = () => {}) {
  return { ...Inspector, setup(props: any, context: any) {
    const state = (Inspector as any).setup(props, context)
    state.selectedAssetJob.value = job
    capture(state)
    return state
  } }
}

if (typeof document !== 'undefined' && document.querySelector('#retry-lineage-fixture')) {
  const selected = ref('original'), blocked = ref(0)
  const block = (): never => { blocked.value++; throw Error('隔离夹具禁止业务网络和真实执行') }
  api.interceptors.request.use(block); api.defaults.adapter = async () => block()
  window.fetch = async () => block()
  XMLHttpRequest.prototype.send = block
  navigator.sendBeacon = block
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
  createApp({ render: () => h('main', { style: 'padding:16px;max-width:1200px;margin:auto' }, [
    h('style', baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g, '')),
    h('h1', '重试链 · 隔离组件验证'), h('p', `只读合成数据；禁止真实生成。已阻断网络请求：${blocked.value}`),
    h(retryInspector(retryFixtureJob(selected.value)), { key: selected.value, onSelect: (id: string) => { selected.value = id } }),
  ]) }).use(createPinia()).use(router).mount('#retry-lineage-fixture')
}
