// M7-07: declared synthetic receipts only. No backend, provider, credentials,
// media downloads, persisted state, or simulated business writes.
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterLink, RouterView, useRoute } from 'vue-router'
import ExecutionPreparationWorkbench from '../../src/components/ExecutionPreparationWorkbench.vue'
import TaskCenterDetail from '../../src/components/TaskCenterDetail.vue'
import { api } from '../../src/services/api'
import type { TaskDetail, TaskKind } from '../../src/services/taskCenter'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

export const ids = { preparation: 'synthetic-journey-preparation', version: 'synthetic-journey-version',
  stage: 'synthetic-journey-stage', project: 'synthetic-journey-project', run: 'synthetic-journey-run',
  a: 'synthetic-journey-attempt-a', b: 'synthetic-journey-attempt-b', job: 'synthetic-journey-asset-job' }
export const choices = [
  ['production', '原诊断 A · 最新 B'], ['draft', '未保存字段草稿'], ['asset', '独立素材 · 确切任务'],
  ['foreign-asset', '独立素材 · 诊断任务错属'], ['missing-task', '诊断缺少任务身份'],
  ['wrong-reply', '详情请求 A · 错返 B'], ['missing-reply', '详情缺少调用身份'],
  ['missing-evidence', '原调用缺少输出证据'],
] as const
export type Mode = typeof choices[number][0]
const stamp = '2026-10-04T01:00:00Z'
const preparationPath = '/fixture/preparation'
const productionPath = `/projects/${ids.project}/production/${ids.run}`
const clone = <T>(value: T): T => structuredClone(value)
export const fixtureStyles = baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g, '')

export function preparationReceipt(mode: Mode = 'production') {
  const asset = mode === 'asset' || mode === 'foreign-asset', draft = mode === 'draft'
  return {
    id: ids.preparation, stage_run: asset ? null : ids.stage, asset_job: asset ? ids.job : null,
    deployment_smoke: null, workflow_smoke: null, execution_instance_version: ids.version, purpose: 'production',
    unit_key: 'shot:synthetic', request_key: 'synthetic-journey-request', revision: 2, status: draft ? 'awaiting_review' : 'dispatched',
    intent_sha256: '1'.repeat(64), request_sha256: draft ? null : 'a'.repeat(64), request_frozen_at: draft ? null : stamp,
    next_check_at: null, reason_code: '', reason: '', evaluation_revision: null, evaluated_at: null, evaluation: null,
    cancellation: asset ? null : { allowed: false, reason: '合成只读夹具：不提供停止或执行操作。', cancelled_at: null, cancelled_by: null },
    presentation: { kind: 'primary', label: '合成准备 · 原诊断', operation: 'video.generate', parent: null,
      task: { id: asset ? ids.job : ids.b, status: 'succeeded', attempt_number: 2, error_code: '' } },
    outcome: { message: '合成最新状态；原诊断 A 的证据必须单独核对。', artifact_count: 0, selected_count: 0 },
    created_by: 1, created_at: stamp, updated_at: stamp, intent: {}, resolved_inputs: {},
    review_contract: { schema_version: 1, mode: 'manual' },
    review_summary: { mode: 'manual', label: '合成审核合同', description: '仅用于原组件导航验证，不执行审核或采用。' },
    prepared_request: null, capacity_sample: null, resource_review: null, text_response_review: null,
    response_diagnostic: mode === 'missing-task'
      ? { state: 'unverifiable', task_id: null, observed_at: null, details: [], summary: '合成诊断缺少原任务身份，无法核验。' }
      : { state: 'recorded', task_id: mode === 'foreign-asset' ? ids.a : asset ? ids.job : ids.a, observed_at: stamp,
        summary: '合成原调用返回未通过校验；这不是最新调用的结果。', details: [{ label: '回执来源', value: '声明的内存合成数据' }] },
    auxiliary_batches: [], field_selections: [],
    field_slots: draft ? [{ unit_key: 'shot:synthetic', field_key: 'journey_text', field_label: '合成待编辑字段',
      next_revision: 1, latest_value_id: null, source_language: 'zh', target_language: 'zh', effective_language: 'zh',
      field_contract: { field_key: 'journey_text', max_chars: 1000, generation_required: true,
        generation_instruction: '仅供验证未保存草稿，不提交。', generation_mode: 'manual', language_policy: 'preserve_source' } }] : [],
  }
}

export function detailReceipt(mode: Mode = 'production', query: Record<string, unknown> = {}): TaskDetail {
  const asset = mode === 'asset', source = asset ? ids.job : String(query.attempt_id || ids.b)
  const original = source === ids.a
  const result: TaskDetail = {
    kind: 'preparation', id: ids.preparation, category: asset ? 'asset' : 'production', title: `合成调用 ${source}`,
    status: original ? 'failed' : 'succeeded', operation: 'video.generate', project_id: asset ? null : ids.project,
    project_title: asset ? null : '合成项目', production_run_id: asset ? null : ids.run,
    execution_instance_version_id: ids.version, execution_instance_name: '合成执行版本', execution_version: 1,
    preparation_id: ids.preparation, source_kind: asset ? 'asset_job' : 'generation_attempt', source_id: source,
    created_at: stamp, updated_at: stamp, finished_at: stamp, estimated_cost: null, actual_cost: null, cost_currency: '',
    anomaly: false, outcome: null, actions: { cancel_asset:false,cancel_smoke:false }, purpose: 'production', preparation_status: 'dispatched',
    request_frozen_at: stamp, request_sha256: (original ? 'a' : 'b').repeat(64),
    submission: { state: 'unavailable', started_at: null, registered_at: null, identifier_state: 'unavailable',
      message: '合成回执：未证明实际发送、远端收到或生产成功。', notes: [] },
    input: { state: 'frozen', submitted: { prompt: `合成原输入 ${source}` }, resolved: {}, parameters: [],
      prompts: [{ key: 'prompt', role: 'user', text: `合成原提示词 ${source}`, source: 'saved_request' }],
      prompt_trace: null, field_batch_trace: null, media_trace: null, api_mapping: null, native_input_trace: null,
      system_message_trace: null, notes: ['内存合成回执，无真实 API、模型或文件读取。'] },
    output: { state: 'available', text: `合成原返回 ${source}`, result: null, media: [], notes: [] },
    validation: { status: original ? 'invalid' : 'unavailable', warnings: [], error_code: original ? 'execution_response_invalid' : '',
      error_message: original ? '合成 A 原返回未通过校验，不以 B 替换。' : '' },
    calls: (asset ? [ids.job] : [ids.a, ids.b]).map(id => ({ id, kind: asset ? 'asset_job' : 'generation_attempt',
      role: 'primary', status: id === ids.a ? 'failed' : 'succeeded', operation: 'video.generate', preparation_id: ids.preparation,
      retry_of_id: null, created_at: stamp, finished_at: stamp, error_code: id === ids.a ? 'execution_response_invalid' : '' })),
    call_count: asset ? 1 : 2, calls_next: null, calls_previous: null, output_count: 0, output_next: null, output_previous: null,
    deferred_fields: [], links: { generation_task_id: null, asset_job_id: asset ? ids.job : null },
  }
  if (mode === 'wrong-reply' && query.attempt_id) return detailReceipt('production', { ...query, attempt_id: ids.b })
  if (mode === 'missing-reply') result.source_id = null
  if (mode === 'missing-evidence') result.output = { state: 'unavailable', text: '', result: null, media: [], notes: ['合成原调用缺少输出证据，不能打开其他成功调用代替。'] }
  return result
}

type Query = Record<string, string | number>
type Transport = { fetch?: typeof fetch; XMLHttpRequest?: { prototype: Pick<XMLHttpRequest, 'send'> }; navigator?: Pick<Navigator, 'sendBeacon'> }
export function isolateHttp(responses: { preparation: () => unknown; detail: (query: Query) => unknown }, transport: Transport = globalThis) {
  const requests = ref<{ method: 'GET'; url: string; query: Query }[]>([]), blocked = ref(0)
  const deny = (): never => { blocked.value++; throw Error('合成夹具禁止未声明请求、业务写入或真实传输') }
  const oldGet = api.get, oldAdapter = api.defaults.adapter
  const interceptor = api.interceptors.request.use(() => deny()) // Runs before the original credential interceptor.
  api.defaults.adapter = async () => deny()
  api.get = (async (url: string, config?: any) => {
    let respond: () => unknown, query: Query = {}
    if (url === `/execution-preparations/${ids.preparation}/` && config === undefined) respond = responses.preparation
    else {
      const q = config?.params
      if (url !== `/task-center/preparation/${ids.preparation}/` || !config || Object.keys(config).some(k => k !== 'params')
        || !q || typeof q !== 'object' || Array.isArray(q) || Object.keys(q).some(k => !['call_page', 'output_page', 'attempt_id'].includes(k))
        || q.call_page !== 1 || q.output_page !== 1 || ('attempt_id' in q && ![ids.a, ids.b].includes(q.attempt_id))) return deny()
      query = { ...q }; respond = () => responses.detail(query)
    }
    requests.value.push({ method: 'GET', url, query: { ...query } })
    return { data: clone(await respond()) }
  }) as typeof api.get
  const originalMethods = new Map<string, unknown>()
  for (const method of ['post', 'put', 'patch', 'delete', 'head', 'options', 'postForm', 'putForm', 'patchForm', 'request']) {
    originalMethods.set(method, (api as any)[method]); (api as any)[method] = async () => deny()
  }
  const originals = { fetch: transport.fetch, send: transport.XMLHttpRequest?.prototype.send, beacon: transport.navigator?.sendBeacon }
  if (originals.fetch) transport.fetch = async () => deny()
  if (originals.send) transport.XMLHttpRequest!.prototype.send = () => deny()
  if (originals.beacon) transport.navigator!.sendBeacon = () => deny()
  return { requests, blocked, restore() {
    api.get = oldGet; api.defaults.adapter = oldAdapter; api.interceptors.request.eject(interceptor)
    for (const [method, value] of originalMethods) (api as any)[method] = value
    if (originals.fetch) transport.fetch = originals.fetch
    if (originals.send) transport.XMLHttpRequest!.prototype.send = originals.send
    if (originals.beacon) transport.navigator!.sendBeacon = originals.beacon
  } }
}

export function isolateStorage(target: typeof globalThis = globalThis) {
  const descriptors = new Map(['localStorage', 'sessionStorage'].map(key => [key, Object.getOwnPropertyDescriptor(target, key)]))
  const forbidden = (): never => { throw Error('合成夹具禁止浏览器持久存储') }
  // The original auth store sees only an empty inert object, never the browser's storage.
  for (const key of descriptors.keys()) Object.defineProperty(target, key, { configurable: true,
    value: { getItem: (name: string) => { if (key === 'localStorage' && ['cineforge_token', 'cineforge_user'].includes(name)) return null; return forbidden() },
      setItem: forbidden, removeItem: forbidden, clear: forbidden, key: forbidden, length: 0 } })
  return () => { for (const [key, descriptor] of descriptors) {
    if (descriptor) Object.defineProperty(target, key, descriptor); else Reflect.deleteProperty(target, key)
  } }
}

export function createFixture(transport: Transport = globalThis) {
  const mode = ref<Mode>('production'), epoch = ref(0), dirty = ref(false), busy = ref(false), failNext = ref(false)
  const events = ref<string[]>([]), pinia = createPinia(), restoreStorage = isolateStorage()
  const responses = {
    preparation: (): unknown => { if (failNext.value) { failNext.value = false; throw Error('合成准备读取失败；上次诊断尚未刷新。') } return preparationReceipt(mode.value) },
    detail: (query: Query): unknown => detailReceipt(mode.value, query),
  }
  const isolation = isolateHttp(responses, transport)
  const Workbench = { setup: () => () => h(ExecutionPreparationWorkbench, { key: epoch.value,
    preparationId: ids.preparation, versionId: ids.version, purpose: 'production',
    onDirty: (value: boolean) => { dirty.value = value }, onBusy: (value: boolean) => { busy.value = value },
    onChanged: () => { events.value.push('unexpected business changed event') },
  }) }
  // Only the workflows query -> detail-props/select wiring is mirrored. The
  // actual SFCs and adapters own identity checks, draft guards and navigation.
  const Details = { setup() {
    const route = useRoute()
    return () => h(TaskCenterDetail, { key: `${route.query.kind}:${route.query.id}:${route.query.attempt_id || ''}`,
      kind: route.query.kind as TaskKind, id: String(route.query.id),
      attemptId: typeof route.query.attempt_id === 'string' ? route.query.attempt_id : undefined,
      expectedProjectId: mode.value === 'asset' ? undefined : ids.project,
      onSelect: (kind: TaskKind, id: string, attemptId?: string) => {
        events.value.push(JSON.stringify({ kind, id, attemptId: attemptId ?? null }))
        void router.push({ name: 'workflows', query: { ...route.query, kind, id, attempt_id: attemptId } })
      }, onChanged: () => { events.value.push('unexpected business changed event') },
    })
  } }
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: preparationPath, name: 'fixture-preparation', component: Workbench },
    { path: productionPath, name: 'fixture-production', component: Workbench },
    { path: '/workflows', name: 'workflows', component: Details },
    { path: `/projects/${ids.project}`, component: { render: () => h('p', '合成项目占位，未加载项目 API。') } },
  ] })
  router.beforeEach(to => {
    if (to.name !== 'workflows') return true
    if (to.query.kind !== 'preparation' || to.query.id !== ids.preparation
      || Object.keys(to.query).some(key => !['kind', 'id', 'attempt_id'].includes(key))
      || (to.query.attempt_id !== undefined && ![ids.a, ids.b].includes(String(to.query.attempt_id)))) throw Error('夹具拒绝未声明路由')
    return true
  })
  async function choose(value: Mode) {
    mode.value = value; epoch.value++; dirty.value = false; busy.value = false; failNext.value = false
    await router.replace(preparationPath)
  }
  function guardBrowserLink(event: MouseEvent) {
    const link = (event.target as Element | null)?.closest('a')
    if (!link) return
    // Normal left-click uses the original RouterLink handler and memory router.
    const href = link.getAttribute('href') || ''
    const allowed = router.resolve(href).matched.length > 0
    if (!allowed || event.type !== 'click' || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
      event.preventDefault(); event.stopPropagation()
    }
  }
  const Root = { setup: () => () => h('main', { class: 'fixture-shell', onClickCapture: guardBrowserLink,
    onAuxclickCapture: guardBrowserLink, onContextmenuCapture: guardBrowserLink }, [
    h('h1', 'M7-07 准备诊断 → 原调用 · 合成内存回执'),
    h('p', '原 SFC、原 API 适配器与 VueRouter。所有 GET 仅回放声明的内存回执；禁止真实 API、凭据、模型、业务写入和媒体下载。不代表后端或生产验收。'),
    h('nav', { class: 'fixture-controls', 'aria-label': '合成场景' }, choices.map(([key, label]) => h('button', {
      type: 'button', class: 'button ghost', 'aria-pressed': mode.value === key, onClick: () => choose(key),
    }, label))),
    h('p', '展开原“查看诊断记录”并点击原证据链接。草稿场景可在原字段表单输入后核对链接消失；取消修改再核对恢复。切换合成场景会丢弃内存草稿。'),
    h('div', { class: 'fixture-controls' }, [
      h('button', { type: 'button', class: 'button ghost', onClick: () => { failNext.value = true } }, '令下一次准备读取失败'),
      h('button', { type: 'button', class: 'button ghost', onClick: () => router.back() }, '内存路由返回'),
      h(RouterLink, { to: preparationPath }, () => '返回准备页（内存）'),
    ]),
    h('p', { role: 'status', 'data-testid': 'journey-status' }, `合成 GET ${isolation.requests.value.length} · 已阻断 ${isolation.blocked.value} · 业务写入 0 · 草稿 ${dirty.value} · 忙碌 ${busy.value}`),
    h('p', { 'data-testid': 'journey-route' }, `内存路由：${router.currentRoute.value.fullPath}`),
    h(RouterView),
    h('details', { class: 'fixture-log' }, [h('summary', '只读请求与导航记录'),
      h('pre', JSON.stringify({ requests: isolation.requests.value, events: events.value }, null, 2))]),
  ]) }
  return { Root, router, pinia, mode, choose, responses, isolation, events, failNext,
    restore() { isolation.restore(); restoreStorage() } }
}

if (typeof document !== 'undefined') {
  const style = document.createElement('style'); style.textContent = fixtureStyles; document.head.append(style)
  const fixture = createFixture(), app = createApp(fixture.Root).use(fixture.pinia).use(fixture.router)
  void fixture.router.push(preparationPath).then(() => fixture.router.isReady()).then(() => app.mount('#app'))
  // Keep network/storage guards installed for the lifetime of this document.
  window.addEventListener('pagehide', () => app.unmount(), { once: true })
}
