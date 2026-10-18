// MOCK only: real page/components; declared read receipts, no model or business writes.
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import ProductionRunView from '../../src/views/ProductionRunView.vue'
import StageExecutionPreparations from '../../src/components/StageExecutionPreparations.vue'
import { api } from '../../src/services/api'
import { platformApi } from '../../src/services/platformApi'
import { useStudioStore } from '../../src/stores/studio'
import type { ProductionRun, StageRun } from '../../src/services/platformTypes'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

export const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
export const originalFailure = 'MOCK 图片生成失败：output node 23 returned no image / 原始失败描述保留。'
const stamp = '2026-10-04T00:00:00Z', sha = 'a'.repeat(64)
export const choices = [
  ['run-a', '原生产页 · Run A 图片失败'], ['run-b', '原生产页 · Run B 图片失败'],
  ['empty', '执行准备列表为空'], ['missing-run', '缺失 Run 身份'],
] as const
type Mode = typeof choices[number][0]
export function records(second = false) {
  const runId = id(second ? 20 : 2), stageId = id(second ? 30 : 3)
  const stage: StageRun = {
    id: stageId, createdAt: stamp, updatedAt: stamp, stageKey: 'bible_image_generation', ordinal: 1,
    mode: 'existing_pipeline', status: 'failed', approvalRequired: false, decisionPolicy: '', decisionPolicySha256: '',
    attemptCount: 1, inputReference: '', inputSnapshotSha256: sha,
    inputSnapshot: { material_requirements: { run_plan_sha256: sha, targets: [
      { requirement_key: `setting:${id(6)}`, appearance_role: 'primary', reference_name: 'MOCK 主图',
        setting: { appearance_id: id(5), setting_revision_id: id(6) } },
    ] }, units: [{ unit_key: `appearance:${id(5)}`, requirement_key: `setting:${id(6)}`, appearance_role: 'primary' }] },
    outputReference: '', outputSnapshot: {}, outputSnapshotSha256: '', reasonCode: 'image_execution_failed',
    reason: originalFailure, routing: {}, routingSha256: '', routingDecision: {}, routingDecisionSha256: '',
    selectedRouteIndex: null, approvedEstimatedCost: '', approvedCostCurrency: '', selectionConfirmedAt: '',
    startedAt: stamp, finishedAt: stamp, approvalDecisions: [], approvalDecisionFacts: [], artifactSelectionDecisions: [],
  }
  const run: ProductionRun = {
    id: runId, createdAt: stamp, updatedAt: stamp, project: id(1), projectTitle: 'MOCK 零产物溯源',
    scriptRevision: id(7), recipeVersion: id(8), planSource: 'recipe', idempotencyKey: 'mock-read-only',
    requestSha256: sha, status: 'failed', currentStageKey: stage.stageKey, blockedCode: stage.reasonCode,
    blockedReason: originalFailure, allowExternalCalls: false, costCurrency: '', estimatedCostTotal: '0',
    committedCostTotal: '0', actualCostTotal: '0', releaseBundleId: '', generationTaskCount: 1,
    configSnapshotSha256: sha, inputSnapshotSha256: sha, startedAt: stamp, finishedAt: stamp,
    stages: [stage], artifacts: [], inputSnapshot: {},
    executionPlan: { schemaVersion: 1, sizeBytes: 1, sha256: sha, value: { recipe: { definition: { schema_version: 3, stages: [
      { key: stage.stageKey, name: 'MOCK 图片生成', kind: 'image', mode: stage.mode, approval: 'auto',
        routing: { mode: 'fixed', routes: [{ execution_instance_version_id: id(9),
          expected_definition_sha256: sha, input_sources: {}, parameter_policies: [] }] } },
    ] } } } },
  }
  const latest = {
    id: id(second ? 40 : 4), stage_run: stageId, asset_job: null, deployment_smoke: null, workflow_smoke: null,
    execution_instance_version: id(9), purpose: 'image.generate', unit_key: `appearance:${id(5)}`,
    request_key: 'mock-original-image-request', revision: 1, status: 'failed', intent_sha256: sha,
    request_sha256: sha, request_frozen_at: stamp, next_check_at: null,
    reason_code: stage.reasonCode, reason: originalFailure, evaluation_revision: 1, evaluated_at: stamp,
    created_by: 1, created_at: stamp, updated_at: stamp,
    presentation: { kind: 'primary', label: 'MOCK 主图 · 图片调用失败', operation: 'image.generate', parent: null,
      task: { id: id(second ? 50 : 10), status: 'failed', error_code: stage.reasonCode, attempt_number: 1 } },
    outcome: { message: '图片调用失败；没有已归档产物，也未采用任何素材。', artifact_count: 0, selected_count: 0 },
  }
  return { run, stage, latest }
}

type Transport = {
  fetch?: typeof fetch
  XMLHttpRequest?: { prototype: Pick<XMLHttpRequest, 'send'> }
  navigator?: Pick<Navigator, 'sendBeacon'>
}
export function createFixture(transport: Transport = globalThis) {
  const mode = ref<Mode>('run-a'), events = ref<string[]>([]), blocked = ref(0), navigationError = ref('')
  const block = (label: string): never => { blocked.value++; events.value.push(`BLOCK ${label}`); throw Error(`MOCK 已阻断未声明请求：${label}`) }
  const oldStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  // Never read the browser's signed-in credentials or mutate its stored session.
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => null } })
  const oldFetch = transport.fetch, oldSend = transport.XMLHttpRequest?.prototype.send, oldBeacon = transport.navigator?.sendBeacon
  if (oldFetch) transport.fetch = async () => block('fetch')
  if (oldSend) transport.XMLHttpRequest!.prototype.send = () => block('XHR.send')
  if (oldBeacon) transport.navigator!.sendBeacon = () => block('sendBeacon')
  const oldAdapter = api.defaults.adapter
  const adapter = async (config: any) => {
    const params = config.params, fixture = records(mode.value === 'run-b')
    if (config.method !== 'get' || config.url !== '/execution-preparations/groups/' || !params
      || params.stage_run_id !== fixture.stage.id || params.page !== 1 || params.kind !== 'primary'
      || Object.keys(params).sort().join() !== 'kind,page,stage_run_id') return block(`${config.method} ${config.url}`)
    events.value.push(`MOCK GET ${config.url} ${JSON.stringify(params)}`)
    const results = mode.value === 'empty' ? [] : [{ unit_key: fixture.latest.unit_key,
      label: fixture.latest.presentation.label, version_count: 1, latest: fixture.latest }]
    return { data: { count: results.length, next: null, previous: null, results }, status: 200, statusText: 'MOCK', headers: {}, config }
  }
  api.defaults.adapter = adapter
  const interceptor = api.interceptors.request.use(config => {
    if (config.adapter !== adapter || config.method !== 'get' || config.url !== '/execution-preparations/groups/') block(`${config.method} ${config.url}`)
    return config
  })
  const pinia = createPinia(), studio = useStudioStore(pinia)
  // Page setup reads are explicit service-boundary mocks. The preparation GET
  // still runs through its original API adapter; this is not backend acceptance.
  const project = { id: id(1), title: 'MOCK 零产物溯源', episodes: [], characters: [], scenes: [], props: [],
    bible: { negativePrompt: '', colorScript: '' }, continuitySummary: '' } as any
  studio.projects = [project]
  studio.loadStyles = async () => { events.value.push('MOCK read styles'); return [] }
  studio.loadProject = async projectId => {
    if (projectId !== project.id) return block(`project ${projectId}`)
    events.value.push(`MOCK read project ${projectId}`); return project
  }
  const oldGetRun = platformApi.getProductionRun
  platformApi.getProductionRun = async runId => {
    const fixture = records(mode.value === 'run-b')
    if (runId !== fixture.run.id) return block(`run ${runId}`)
    events.value.push(`MOCK read run ${runId}`); return structuredClone(fixture.run)
  }
  const empty = { render: () => null }
  const Stage = { setup: () => () => h(StageExecutionPreparations, {
    stageId: records().stage.id, runId: mode.value === 'missing-run' ? undefined : records().run.id,
  }) }
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: empty }, { path: '/stage', name: 'fixture-stage', component: Stage },
    { path: '/projects/:projectId/production/:runId', name: 'production-run-detail', component: ProductionRunView },
    { path: '/projects/:projectId/production', name: 'production-run', component: empty },
    { path: '/projects/:projectId', name: 'project', component: empty },
    { path: '/workflows', name: 'workflows', component: empty },
  ] })
  const choose = async (value: Mode) => {
    if (!choices.some(([key]) => key === value)) return block('unknown scenario')
    mode.value = value; navigationError.value = ''
    const { run } = records(value === 'run-b')
    await router.push(value.startsWith('run-') ? { name: 'production-run-detail', params: { projectId: run.project, runId: run.id } } : { name: 'fixture-stage' })
  }
  function navigate(event: MouseEvent) {
    const link = (event.target as Element | null)?.closest('a')
    if (!link) return
    event.preventDefault(); event.stopPropagation()
    if (event.type === 'contextmenu') return
    const href = link.getAttribute('href') || ''
    if (!/^\/workflows\?/.test(href)) { navigationError.value = '已阻断非任务夹具导航'; return }
    return router.push(href)
  }
  const Root = { setup: () => () => h('main', { class: 'provenance-fixture', onClickCapture: navigate,
    onAuxclickCapture: navigate, onContextmenuCapture: navigate }, [
    h('style', baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g, '')),
    h('h1', 'MOCK 隔离模拟 · 零模型 / 零现用业务'),
    h('p', '真实生产页与执行准备组件；全部记录为合成回执。所有业务写入、未声明读取及网络出口均阻断。请仅检查失败说明与原调用链接，不执行重试。'),
    h('nav', choices.map(([value, label]) => h('button', { class: 'button ghost', type: 'button',
      'aria-pressed': mode.value === value, onClick: () => choose(value) }, label))),
    h('p', { role: 'status' }, `未声明请求计数：${blocked.value}`),
    navigationError.value ? h('p', { role: 'alert' }, navigationError.value) : null,
    router.currentRoute.value.name === 'workflows' ? h('section', [
      h('h2', '内存任务定位（不加载真实任务页）'), h('pre', JSON.stringify(router.currentRoute.value.query, null, 2)),
      h('button', { class: 'button primary', onClick: () => choose(mode.value) }, '返回原隔离场景'),
    ]) : h(RouterView, { key: mode.value }),
    h('details', [h('summary', 'MOCK 请求日志'), h('pre', events.value.join('\n'))]),
  ]) }
  function restore() {
    platformApi.getProductionRun = oldGetRun; api.defaults.adapter = oldAdapter; api.interceptors.request.eject(interceptor)
    if (oldFetch) transport.fetch = oldFetch
    if (oldSend) transport.XMLHttpRequest!.prototype.send = oldSend
    if (oldBeacon) transport.navigator!.sendBeacon = oldBeacon
    if (oldStorage) Object.defineProperty(globalThis, 'localStorage', oldStorage); else Reflect.deleteProperty(globalThis, 'localStorage')
  }
  return { Root, pinia, router, choose, navigate, mode, events, blocked, restore }
}

if (typeof document !== 'undefined' && document.getElementById('app')) {
  const fixture = createFixture()
  void fixture.choose('run-a').then(() => createApp(fixture.Root).use(fixture.pinia).use(fixture.router).mount('#app'))
}
