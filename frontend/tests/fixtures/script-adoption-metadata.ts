import { createApp, h, ref } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import ProductionTextCandidateReview from '../../src/components/ProductionTextCandidateReview.vue'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import '../../src/styles.css'

const hash = 'a'.repeat(64)
const producer = { id: 'fixture-source', ordinal: 1, status: 'succeeded', stageKey: 'script_generate', outputSnapshotSha256: hash }
const review = { id: 'fixture-review', ordinal: 2, stageKey: 'script_review', inputSnapshotSha256: hash,
  inputSnapshot: { candidate_source: { stage_id: producer.id, stage_key: producer.stageKey, output_snapshot_sha256: hash } } }
const run = { id: 'fixture-run', project: 'fixture-project', planSource: 'recipe', status: 'awaiting_approval', stages: [producer, review] }
const listRoot = ref(false)
const rejectEdit = ref(false)
const fields = { type: 'object', properties: { title: { type: 'string', title: '候选名称' }, logline: { type: 'string', title: '故事简介' } }, required: ['title','logline'] }
const objectCandidate = { title: '模型提出的另一名称', logline: '候选中的故事简介，不回写项目资料。' }
const listCandidate = [{ ...objectCandidate, custom: { count: 0, enabled: false, optional: null } }]
const artifact = () => ({ id: 'fixture-candidate', productionRun: run.id, stageRun: producer.id, variantIndex: 1,
  status: 'available', sha256: hash, manifestMetadataSha256: hash, createdAt: '2026-09-30T06:00:00Z', generationAttempt: 'fixture-attempt',
  textCandidate: { operation: 'script.generate', candidate: listRoot.value ? listCandidate : objectCandidate,
    json_schema: listRoot.value ? { type: 'array', items: fields } : fields,
    text: listRoot.value ? `  ${JSON.stringify(listCandidate, null, 2)}\r\n` : '夜晚，旅人推开阅览室的门。\n镜头缓缓向前，停在桌上的一本旧书。',
    warnings: listRoot.value ? ['[0].custom：自定义字段保留在原始字段值中。'] : [] } })
const receipt = ref<unknown>(null)
// Only this isolated fixture replaces API calls. No real request or mutation.
api.interceptors.request.use(() => { throw Error('隔离页面禁止真实API请求') })
platformApi.listArtifactPage = async () => ({ results: [artifact()], count: 1, hasNext: false, hasPrevious: false }) as any
platformApi.getArtifact = async () => artifact() as any
platformApi.approveProductionRun = async (_id, input) => {
  if (rejectEdit.value) throw { response: { status: 400, data: { error: { code: 'validation_error', message: '隔离关系校验失败', details: { 'source.title': ['请核对设定引用'] } } } } }
  receipt.value = input; return run as any
}
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } },
  { path: '/workflows', name: 'workflows', component: { render: () => null } }] })
createApp({ setup: () => () => h('main', { style: 'max-width:960px;margin:16px auto;padding:16px;min-width:0' }, [
  h('h1', '剧本采用 · 隔离页面样例'), h('p', '仅内存样例，不调用业务API或模型，不保存、审核或采用真实数据。'),
  h('p', '项目原资料：项目名称「图书馆来客」；梗概「旅人探访旧图书馆」。候选名称与此不同。'),
  h('label', [h('input', { type: 'checkbox', checked: rejectEdit.value, onChange: (event: Event) => { rejectEdit.value = (event.target as HTMLInputElement).checked } }), ' 模拟字段校验失败（仅内存）']),
  h('label', [h('input', { type: 'checkbox', checked: listRoot.value, onChange: (event: Event) => {
    listRoot.value = (event.target as HTMLInputElement).checked
    receipt.value = null
  } }), ' 根数组候选（含 0 / false / null 自定义字段）']),
  h(ProductionTextCandidateReview, { key: String(listRoot.value), run: run as any, stage: review as any, currentRules: {} }),
  receipt.value ? h('section', { 'aria-label': '隔离提交结果' }, [h('h2', '仅捕获提交参数'),
    h('p', '项目原资料保持不变。此样例不证明真实API采用通过。'),
    h('pre', { style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, JSON.stringify(receipt.value, null, 2))]) : null,
]) }).use(router).mount('#app')
