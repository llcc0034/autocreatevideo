import { createApp, h, ref } from 'vue'
import { api } from '../../src/services/api'
import ExecutionStorageMetrics from '../../src/components/ExecutionStorageMetrics.vue'
import { preparationId, storageReceipt } from './execution-storage-metrics'
import '../../src/styles.css'
import '../../src/platform.css'

const revision = ref(8), failed = ref(false), narrow = ref(false), reads = ref(0)
const adapter = async (config: any) => {
  if (config.method !== 'get' || config.url !== `/execution-preparations/${preparationId}/storage-metrics/`
    || config.data !== undefined || Object.keys(config.params).join() !== 'expected_preparation_revision') throw Error('隔离夹具禁止真实业务请求')
  reads.value++
  if (failed.value) throw Error('隔离错误：字节元数据不可读')
  return { data: storageReceipt(config.params.expected_preparation_revision), status: 200, statusText: 'OK', headers: {}, config }
}
api.interceptors.request.clear(); api.defaults.adapter = adapter
api.interceptors.request.use(config => { config.adapter = adapter; config.headers.delete('Authorization'); return config })
createApp({ setup: () => () => h('main', { style: { maxWidth: narrow.value ? '390px' : '1000px', padding: '16px', margin: '16px auto', minWidth: '0' } }, [
  h('h1', { style: { fontSize: '24px' } }, '隔离：准备字节明细'),
  h('p', '正式组件＋本地 API 夹具，不连接业务库、不调用模型；数值不是现用业务证据。'),
  h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } }, [
    h('button', { class: 'button', onClick: () => { narrow.value = !narrow.value } }, narrow.value ? '宽屏' : '390px 窄屏'),
    h('button', { class: 'button', onClick: () => { failed.value = !failed.value } }, failed.value ? '恢复读取' : '模拟读取失败'),
    h('button', { class: 'button', onClick: () => { revision.value++ } }, '切换准备修订'),
  ]),
  h('p', { role: 'status' }, `当前修订 ${revision.value} · 夹具 GET ${reads.value} 次 · 真实请求 0`),
  h(ExecutionStorageMetrics, { preparationId, revision: revision.value, disabled: false }),
]) }).mount('#app')
