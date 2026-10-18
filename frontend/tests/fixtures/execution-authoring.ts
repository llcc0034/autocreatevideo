import { createApp, h, ref } from 'vue'
import ExecutionVersionEditor from '../../src/components/ExecutionVersionEditor.vue'
import BaseModal from '../../src/components/BaseModal.vue'
import WorkflowVersionEditorView from '../../src/views/WorkflowVersionEditorView.vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { createPinia } from 'pinia'
import { executionRegistryApi } from '../../src/services/executionRegistryApi'
import { platformApi } from '../../src/services/platformApi'
import { repository } from '../../src/services/repository'
import { adaptWorkflowTemplateVersion } from '../../src/services/platformAdapters'
import { api } from '../../src/services/api'
import { isolatedMaintenanceDestination } from './exact-maintenance-window'
import * as originalData from './execution-authoring-data'
import '../../src/styles.css'
import '../../src/platform.css'

const { instance, version, deployments, workflows, profiles, choices, authoring } = structuredClone({
  instance: originalData.instance, version: originalData.version, deployments: originalData.deployments,
  workflows: originalData.workflows, profiles: originalData.profiles, choices: originalData.choices,
  authoring: originalData.authoring,
})
// The real workflow maintenance view validates UUID syntax; keep existing fixture
// scenarios unchanged and use one explicit valid synthetic identity for this path.
if (new URLSearchParams(location.search).has('maintenance')) {
  const workflowId = '51000000-0000-4000-8000-000000000001'
  workflows[0]!.id = workflowId
  version.workflow_version_id = workflowId
  version.configuration.workflow_version_id = workflowId
}
// Test-only in-memory writer responses. All network transports are denied.
const blocked = ref(0), writes = ref(0), failSave = ref(false), selected = ref<any>(structuredClone(version)), editorKey = ref(0), open = ref(true)
const resultMode = ref('success'), reads = ref(0), serverVersion = ref<any>(structuredClone(version)), familyRevision = ref(instance.revision)
const snapshot = (value: any) => JSON.parse(JSON.stringify(value))
function deny(): never { blocked.value++; throw Error('隔离夹具禁止真实网络请求') }
api.interceptors.request.use(() => deny()); api.defaults.adapter = async () => deny()
window.fetch = async () => deny(); XMLHttpRequest.prototype.send = () => deny(); navigator.sendBeacon = () => deny()
executionRegistryApi.choices = async deployment => structuredClone(deployment ? choices : { ...choices, target: null }) as any
executionRegistryApi.authoring = async () => structuredClone(authoring) as any
platformApi.listPromptProfileVersions = async () => profiles as any
platformApi.listWorkflowVersions = async () => workflows as any
platformApi.getPromptProfileOptions = async () => ({ sourceSelectors: [] }) as any
const resourceUrl = (kind: string, id: string) => `/settings/acceptance-smoke?view=relations&kind=${kind}&ids=${id}`
const resourceRow = (kind: string, id: string) => ({ id, kind, label: `隔离${kind} · 精确旧版本`, status: 'published', config_url: kind === 'workflow' ? `/settings/workflows/${id}` : `/settings?section=workflows&workflow_version=${id}`, overview_url: resourceUrl(kind, id), acceptance_url: '', capabilities: [], notices: [], groups: kind === 'workflow' ? [{ key: 'projector', kind: 'projector', label: '引用 Projector', count: 1, url: `/settings/acceptance-smoke?view=relations&kind=projector&source_kind=workflow&source_id=${id}&relation=projector` }] : [] })
api.get = async (url: string, options: any) => {
  if (url !== '/registry-relations/') return deny()
  reads.value++
  const { kind, ids } = options.params
  const rows = [resourceRow(kind, ids || 'fixture-projector')]
  return { data: { count: 1, next: null, previous: null, generated_at: '2026-10-09T02:15:00Z', context_label: '', results: rows } } as any
}
platformApi.getDeployment = async id => { reads.value++; return { ...deployments[0], id, profileVersion: 'fixture-model', priority: 0, isDefault: false, credentialConfigured: false } as any }
platformApi.getModelProfile = async id => { reads.value++; return { id, profileKey: '隔离参考模型', version: 1, status: 'published', modelIdentifier: 'fixture', modality: 'video', providerKey: 'fixture', specification: { inputs: ['reference_pixels'], outputs: ['video'], variable: ['sample.scale'] } } as any }
platformApi.getWorkflowVersion = async id => { if (id !== workflows[0]!.id) return deny(); reads.value++; return { ...adaptWorkflowTemplateVersion({}), ...workflows[0], id, inputContract: { reference_pixels: { kind: 'image', min_items: 1, max_items: 2 }, parameters: { 'sample.scale': { type: 'number', default: 0 } } }, outputKind: 'video', validationState: 'pending', validationMessage: '隔离样例，不代表已校验', smokeTestState: 'pending', smokeTestMessage: '未发起真实调用', graph: { fixture: { inputs: {}, class_type: 'fixture_only' } } } as any }
platformApi.getWorkflowProjectorVersion = async id => { reads.value++; return { id, projectorKey: '隔离映射方案', version: 1, status: 'published', engineKey: 'fixture', engineVersion: 1, providerDriverType: 'fixture', operation: 'fixture.reference', outputKind: 'video', definition: { inputs: { reference_pixels: 'fixture_only' } } } as any }
platformApi.getPromptProfileVersion = async id => { reads.value++; return { ...profiles[0], id, description: '隔离字段→处理→通道', sourceProjectionKey: 'fixture', sourceProjectionConfig: {}, inputStructureVersion: null, fieldRuleCount: 1, channelLimits: { positive_prompt: { max_chars: 2000 } } } as any }
platformApi.listPromptProfileFieldRules = async id => { reads.value++; return [{ id: 'fixture-rule', profileVersion: id, fieldKey: 'custom.text', fieldLabel: '动作描述', position: 0, channel: 'positive_prompt', generationMode: 'passthrough', hasDefault: false, required: true }] as any }
executionRegistryApi.version = async id => { reads.value++; if (serverVersion.value.id !== id) throw Error('fixture identity mismatch'); return snapshot(serverVersion.value) }
executionRegistryApi.instance = async () => { reads.value++; return { ...instance, revision: familyRevision.value } as any }
executionRegistryApi.versions = async () => { reads.value++; return { results: [snapshot(serverVersion.value)], next: null, previous: null, count: 1 } }
async function save(configuration: any, fresh: boolean) {
  writes.value++; await new Promise(resolve => setTimeout(resolve, 250))
  if (failSave.value || resultMode.value === 'reject') throw { response: { status: 400, data: { message: '隔离校验拒绝，内容保留', fields: { 'parameters.0.default_value': ['测试范围错误，请核对参数'] } } } }
  if (resultMode.value === 'timeout-unwritten') throw Error('隔离网络超时：结果未知')
  serverVersion.value = { ...version, id: fresh ? 'fixture-created' : selected.value.id, version: fresh ? 3 : selected.value.version, revision: fresh ? 1 : selected.value.revision + 1, ...configuration, configuration }
  if (fresh) familyRevision.value++
  if (resultMode.value === 'timeout-written') throw { response: { status: 504 } }
  return snapshot(serverVersion.value)
}
executionRegistryApi.createVersion = async (_id, _revision, configuration) => save(configuration, true)
executionRegistryApi.updateVersion = async (_id, patch) => { const { expected_revision: ignored, ...changes } = patch; return save({ ...selected.value.configuration, ...changes }, false) }
function switchMode(mode: string) {
  selected.value = mode === 'new' ? null : { ...structuredClone(version), ...(mode === 'successor' ? { id: 'fixture-successor', version: 3, revision: 1 } : {}) }
  serverVersion.value = snapshot(selected.value || version); familyRevision.value = instance.revision
  editorKey.value++; open.value = true
}
const maintenanceParams = new URLSearchParams(location.search)
const exactWorkflowRoute = '/settings/workflows/' + workflows[0]!.id + '?relation_context=' + encodeURIComponent('workflow:' + workflows[0]!.id)
const maintenanceRoute = maintenanceParams.get('maintenance_route') || ''
if (maintenanceRoute && maintenanceRoute !== exactWorkflowRoute) deny()
if (maintenanceParams.has('maintenance')) {
  document.addEventListener('click', event => {
    const link = (event.target as Element)?.closest('a[target="_blank"]') as HTMLAnchorElement | null
    if (!link) return
    event.preventDefault()
    let destination: string
    try { destination = isolatedMaintenanceDestination(link.href, location.href, exactWorkflowRoute) }
    catch { return deny() }
    window.open(destination, '_blank', 'noopener')
  })
  for (const key of ['listDeployments', 'listProviders', 'listModelProfiles', 'listWorkflowSmokeAttempts']) {
    (platformApi as any)[key] = async () => { reads.value++; return [] }
  }
  platformApi.getAcceptanceSmokePage = async () => { reads.value++; return { count: 1, next: null, previous: null, results: [{ subject: 'workflow', workflow: { id: workflows[0]!.id }, validation: { state: 'not_run', fresh: false, evidenceSha256: '' }, smoke: { fresh: false } }] } as any }
  repository.getProductionCapability = async () => { reads.value++; return { effectiveLimits: { auditNoteMinCharacters: 1, auditNoteMaxCharacters: 1000 } } as any }
}
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings/workflows/:workflowVersionId', component: WorkflowVersionEditorView }] })
await router.push(exactWorkflowRoute); await router.isReady()
createApp({ setup() { return () => maintenanceRoute ? h('main', { style: 'padding:16px;max-width:1120px;margin:auto;min-width:0' }, [
  h('h1', '精确工作流维护 · 隔离只读'),
  h('p', { 'data-fixture-evidence': '' }, `原维护入口 ${maintenanceRoute} · 写入 ${writes.value} · 只读 ${reads.value} · 网络守卫 ${blocked.value}`),
  h(WorkflowVersionEditorView),
]) : h('main', { style: 'padding:16px;max-width:1120px;margin:auto' }, [
  h('h1', '执行实例分层作者 · 隔离验证'),
  h('p', `隔离提交 ${writes.value} · 只读核对 ${reads.value} · 拦截网络 ${blocked.value} · 不写现用库`),
  h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' }, [
    ...[['draft', '编辑草稿'], ['new', '新建版本'], ['successor', '后继草稿']].map(([key, label]) => h('button', { type: 'button', class: 'button ghost', onClick: () => switchMode(key!) }, label)),
    h('label', [h('input', { type: 'checkbox', checked: failSave.value, onChange: (event: Event) => { failSave.value = (event.target as HTMLInputElement).checked } }), '模拟保存失败']),
    h('label', ['保存结果场景', h('select', { value: resultMode.value, onChange: (event: Event) => { resultMode.value = (event.target as HTMLSelectElement).value } }, ['success', 'reject', 'timeout-unwritten', 'timeout-written'].map(mode => h('option', { value: mode }, mode)))]),
  ]),
  h(BaseModal, { configuration: true, open: open.value, title: selected.value ? `隔离参考生视频 · v${selected.value.version}` : '新建执行实例版本', width: '1120px', onClose: () => { open.value = false } }, {
    default: () => h(ExecutionVersionEditor, { key: editorKey.value, instance: instance as any, version: selected.value, deployments: deployments as any,
      onSaved: (value: any) => { selected.value = value; editorKey.value++ }, onRecovered: (value: any) => { selected.value = value; editorKey.value++ }, onReview: () => {} }),
  }),
]) } }).use(createPinia()).use(router).mount('#app')
