import { createApp, h, ref } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import ExecutionInstanceRegistry from '../../src/components/ExecutionInstanceRegistry.vue'
import { executionRegistryApi } from '../../src/services/executionRegistryApi'
import { platformApi } from '../../src/services/platformApi'
import { api } from '../../src/services/api'
import { instance, version, deployments, workflows, profiles } from './execution-authoring-data'
import { auxiliaryVersion, derivedInput, modeScenarios, scenarioChoices, scenarioAuthoring, scenarioContract } from './execution-version-scenarios'
import '../../src/styles.css'
import '../../src/platform.css'

// Browser authoring fixture: original components/writer boundaries, in-memory data only.
const blocked = ref(0), writes = ref(0), reads = ref(0), resultMode = ref('success'), resetKey = ref(0)
const families: any[] = [structuredClone(instance), ...modeScenarios.map(row => structuredClone(row.family))], versions: any[] = [structuredClone(version), ...modeScenarios.map(row => structuredClone(row.source))]
const receipts = ref<any[]>([])
const snap = (value: any) => JSON.parse(JSON.stringify(value))
const page = (rows: any[]) => ({ count: rows.length, next: null, previous: null, results: snap(rows) })
function deny(): never { blocked.value++; throw Error('隔离夹具禁止真实网络请求') }
api.interceptors.request.use(() => deny()); api.defaults.adapter = async () => deny()
window.fetch = async () => deny(); XMLHttpRequest.prototype.send = () => deny(); navigator.sendBeacon = () => deny()
for (const client of [executionRegistryApi, platformApi]) for (const key of Object.keys(client)) (client as any)[key] = async () => deny()
api.get = async (url: string, options: any) => {
  if (url !== '/registry-relations/') return deny()
  reads.value++
  const rows = String(options.params.ids || '').split(',').filter(Boolean).map(id => ({ id, kind: options.params.kind, label: '隔离精确引用', status: versions.find(row => row.id === id)?.status || 'published', config_url: `/settings?section=executions&execution_version=${id}`, overview_url: `/settings/acceptance-smoke?view=relations&kind=${options.params.kind}&ids=${id}`, acceptance_url: '', capabilities: [], notices: [], groups: [] }))
  return { data: { ...page(rows), generated_at: '2026-10-10T02:00:00+08:00', context_label: '' } } as any
}
executionRegistryApi.choices = async deployment => { reads.value++; return snap(scenarioChoices(deployment)) }
executionRegistryApi.authoring = async id => { reads.value++; return snap(scenarioAuthoring(versions.find(row => row.id === id))) }
platformApi.listPromptProfileVersions = async () => snap(profiles)
platformApi.listWorkflowVersions = async () => snap(workflows)
platformApi.getPromptProfileOptions = async () => ({ sourceSelectors: [] }) as any
executionRegistryApi.instances = async (_page, search) => { reads.value++; return page(families.filter(row => !search || row.key.includes(search) || row.name.includes(search))) }
executionRegistryApi.instance = async id => { reads.value++; const row = families.find(row => row.id === id); if (!row) throw Error('实例不存在'); return snap(row) }
executionRegistryApi.versions = async (_page, filters) => { reads.value++; return page([...versions, auxiliaryVersion].filter(row => (!filters?.instance || row.instance_id === filters.instance) && (!filters?.operation || row.operation === filters.operation) && (!filters?.status || row.status === filters.status))) }
executionRegistryApi.version = async id => { reads.value++; const row = versions.find(row => row.id === id) || (id === auxiliaryVersion.id ? auxiliaryVersion : null); if (!row) throw Error('版本不存在'); return snap(row) }
async function write(action: () => any) {
  writes.value++
  const mode = resultMode.value
  await new Promise(resolve => setTimeout(resolve, 300))
  if (mode === 'reject') throw { response: { status: 400, data: { message: '隔离明确拒绝，原输入保留' } } }
  if (mode === 'timeout-unwritten') throw Error('隔离超时，写入结果未知')
  const result = action()
  if (mode === 'timeout-written') throw { response: { status: 504 } }
  return snap(result)
}
executionRegistryApi.createInstance = payload => write(() => {
  if (families.some(row => row.key === payload.key)) throw { response: { status: 400, data: { message: '实例标识重复' } } }
  const row = { ...instance, ...snap(payload), id: `fixture-created-family-${writes.value}`, revision: 1 }; families.push(row); return row
})
executionRegistryApi.updateInstance = (id, patch) => write(() => {
  const row = families.find(row => row.id === id); if (!row || row.revision !== patch.expected_revision) throw { response: { status: 409 } }
  const { expected_revision, ...changes } = patch; Object.assign(row, snap(changes), { revision: expected_revision + 1 }); return row
})
executionRegistryApi.createVersion = (id, revision, configuration) => write(() => {
  receipts.value.push({ action: 'create', id, revision, payload: snap(configuration) })
  const owner = families.find(row => row.id === id); if (!owner || owner.revision !== revision) throw { response: { status: 409 } }
  const row = { ...version, ...snap(configuration), id: `fixture-created-version-${writes.value}`, instance_id: id, instance_name: owner.name, version: 1 + Math.max(0, ...versions.filter(row => row.instance_id === id).map(row => row.version)), revision: 1, configuration: snap(configuration) }
  if (configuration.prompt_mode === 'profile') row.configuration.inputs = [...row.configuration.inputs, snap(derivedInput)]
  owner.revision++; versions.push(row); return row
})
executionRegistryApi.updateVersion = (id, patch) => write(() => {
  receipts.value.push({ action: 'patch', id, payload: snap(patch) })
  const row = versions.find(row => row.id === id); if (!row || row.revision !== patch.expected_revision) throw { response: { status: 409 } }
  const { expected_revision, ...changes } = patch; Object.assign(row.configuration, snap(changes)); Object.assign(row, snap(changes), { revision: expected_revision + 1 })
  if (row.configuration.prompt_mode === 'profile' && !row.configuration.inputs.some((input: any) => input.key === derivedInput.key)) row.configuration.inputs.push(snap(derivedInput))
  return row
})
executionRegistryApi.clone = (id, payload) => write(() => {
  const source = versions.find(row => row.id === id)
  receipts.value.push({ action: 'clone', id, payload: snap(payload) })
  if (!source || source.revision !== payload.expected_revision || payload.expected_definition_sha256 !== (source.status === 'draft' ? null : scenarioContract(source).sha256)) throw { response: { status: 409 } }
  const row = { ...snap(source), id: `fixture-successor-${writes.value}`, version: 1 + Math.max(...versions.filter(row => row.instance_id === source.instance_id).map(row => row.version)), revision: 1, status: 'draft' }
  versions.push(row); families.find(row => row.id === source.instance_id).revision++; return row
})
executionRegistryApi.contract = async id => { reads.value++; const row = versions.find(row => row.id === id) || (id === auxiliaryVersion.id ? auxiliaryVersion : null); if (!row) throw Error('版本不存在'); return snap(scenarioContract(row)) as any }
platformApi.getPromptProfileVersion = async id => { reads.value++; if (id !== profiles[0]!.id) return deny(); return { ...snap(profiles[0]), fieldRuleCount: 1, description: '隔离策略引用', sourceProjectionKey: 'fixture', sourceProjectionConfig: {}, inputStructureVersion: null, channelLimits: {} } as any }
platformApi.listPromptProfileFieldRules = async id => { reads.value++; return [{ id: 'fixture-rule', profileVersion: id, fieldKey: 'direction', fieldLabel: '运镜', position: 0, channel: 'positive_prompt', generationMode: 'generate', required: true }] as any }
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { render: () => null } }, { path: '/settings/acceptance-smoke', component: { render: () => h('p', '隔离关联目录占位，不执行任务') } }, { path: '/workflows', name: 'workflows', component: { render: () => h('p', '隔离占位，不执行任务') } }] })
await router.push('/settings?section=executions')
createApp({ setup: () => () => h('main', { style: 'padding:16px;max-width:1200px;margin:auto' }, [
  h('h1', '执行实例新建与后继 · 隔离验证'),
  h('p', { role: 'status', 'data-fixture-evidence': '' }, `隔离写入 ${writes.value} · 只读 ${reads.value} · 网络拦截 ${blocked.value} · 内存版本 ${versions.length} · 不写现用库`),
  h('label', ['保存结果场景', h('select', { value: resultMode.value, onChange: (event: Event) => { resultMode.value = (event.target as HTMLSelectElement).value } }, ['success', 'reject', 'timeout-unwritten', 'timeout-written'].map(value => h('option', { value }, value)))]),
  h('button', { class: 'button ghost', onClick: () => { resetKey.value++ } }, '重开目录（保留隔离记录）'),
  h(ExecutionInstanceRegistry, { key: resetKey.value, deployments: [...deployments, { ...deployments[0], id: 'fixture-direct', name: '隔离直接接口部署' }] as any }),
  h('details', [h('summary', '隔离请求与原来源核对'), h('pre', { 'data-fixture-receipts': '', style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, JSON.stringify({ receipts: receipts.value, sourcesUnchanged: modeScenarios.every(row => JSON.stringify(versions.find(source => source.id === row.source.id)) === JSON.stringify(row.source)), versions }, null, 2))]),
]) }).use(router).mount('#app')
