// Generated isolated API receipts; no invented approval/call result or live API.
import {createApp, h, ref, computed} from 'vue'
import {api} from '../../src/services/api'
import ExecutionPromptDisplay from '../../src/components/ExecutionPromptDisplay.vue'
import ExecutionStorageMetrics from '../../src/components/ExecutionStorageMetrics.vue'
import FrozenReferenceCorrespondence from '../../src/components/FrozenReferenceCorrespondence.vue'
import {adaptFrozenReferenceBinding} from '../../src/services/frozenReferenceBinding'
import mixed from '../../../.artifacts/batch307/m6-receipts/mixed.json'
import overflow from '../../../.artifacts/batch307/m6-receipts/overflow.json'
import '../../src/styles.css'
import '../../src/platform.css'

const scenario = ref('mixed'), position = ref(0), failed = ref(false), readCount = ref(0)
const receipt = computed<any>(() => (scenario.value === 'mixed' ? mixed : overflow)[position.value]!)
const adapter = async (config: any) => {
  const current = receipt.value
  if (config.method !== 'get' || config.data !== undefined
    || Object.keys(config.params || {}).join() !== 'expected_preparation_revision'
    || config.params.expected_preparation_revision !== current.preparation.revision) throw Error('隔离页禁止实际写入或错误修订')
  const base = `/execution-preparations/${current.preparation.id}/`
  const data = config.url === base + 'prompt-display/' ? current.prompt
    : config.url === base + 'storage-metrics/' ? current.metrics : null
  if (!data) throw Error('隔离页禁止未声明API')
  readCount.value++
  if (failed.value) throw Error('隔离错误：原准备读取失败')
  return {config, data: structuredClone(data), status: 200, statusText: 'OK', headers: {}}
}
api.defaults.adapter = adapter
api.interceptors.request.use(config => {config.adapter = adapter; config.headers.delete('Authorization'); return config})
createApp({setup: () => () => h('main', {style: {maxWidth: '1050px', minWidth: 0, margin: '16px auto', padding: '16px'}}, [
  h('h1', '隔离：M6 混合用途与超限全文'),
  h('p', '正式组件读取本批通过断言的隔离API回执。下方切换是查看已记录状态，不是现用审核、模型调用或真实容量认证；业务网络已阻断。'),
  h('div', {style: {display: 'flex', gap: '12px', flexWrap: 'wrap'}}, [
    h('label', ['组合 ', h('select', {value: scenario.value, onChange: (event: Event) => {
      scenario.value = (event.target as HTMLSelectElement).value; position.value = 0
    }}, [h('option', {value: 'mixed'}, '图片 / 双视频 / 同音频两个用途'), h('option', {value: 'overflow'}, 'Unicode 累计超限')])]),
    h('label', ['已记录状态 ', h('select', {value: position.value, onChange: (event: Event) => {
      position.value = Number((event.target as HTMLSelectElement).value)
    }}, (scenario.value === 'mixed' ? ['原顺序', '视频重排'] : ['字段候选', '正式确认后', '编译受阻后']).map((label, index) => h('option', {value: index}, label)))]),
    h('button', {class: 'button', onClick: () => {failed.value = !failed.value}}, failed.value ? '恢复读取' : '模拟读取失败'),
  ]),
  h('p', {role: 'status'}, `原准备修订 ${receipt.value.preparation.revision} · 隔离读取 ${readCount.value} 次 · 真实请求 0`),
  h('details', [h('summary', '核对原素材与独立用途（按需）'),
    ...Object.entries(receipt.value.correspondence || {}).map(([key, value]) => {
      const media = receipt.value.media.find((row: any) => row.item_key === key)!
      return h('article', {key: receipt.value.preparation.id + key, style: {margin: '12px 0', minWidth: 0}}, [
        h('p', `${media.label} · 本地测试信号原字节（非生成结果）`),
        media.kind === 'image' ? h('img', {src: media.data_url, alt: `${media.label} 隔离参考图`, style: {width: '72px', height: '72px'}})
          : h(media.kind === 'video' ? 'video' : 'audio', {src: media.data_url, controls: true, preload: 'none',
            style: {maxWidth: '100%', width: '300px'}}),
        h(FrozenReferenceCorrespondence, {binding: adaptFrozenReferenceBinding(value)}),
      ])
    }),
  ]),
  h(ExecutionPromptDisplay, {preparationId: receipt.value.preparation.id, revision: receipt.value.preparation.revision,
    frozen: false, disabled: false, fieldLabels: {configured_action: '动作', unit_description: '逐镜描述'}}),
  h(ExecutionStorageMetrics, {preparationId: receipt.value.preparation.id, revision: receipt.value.preparation.revision, disabled: false}),
])}).mount('#app')
