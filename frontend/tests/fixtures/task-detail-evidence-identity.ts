// Synthetic read-only API receipts. No model, backend, credentials or business writes.
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import TaskCenterDetail from '../../src/components/TaskCenterDetail.vue'
import { api } from '../../src/services/api'
import type { TaskDetail, TaskKind } from '../../src/services/taskCenter'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

export const fixtureStyles = baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g, '')
export const ids = { preparation: 'synthetic-preparation', other: 'synthetic-other-preparation', project: 'synthetic-project', a: 'synthetic-attempt-a', b: 'synthetic-attempt-b' }
export const choices = [
  ['correct', '指定 A · 正确回执'], ['wrong-attempt', '指定 A · 错返 B'],
  ['wrong-kind', '相同 ID · 错误来源类型'], ['missing-source', '缺失调用身份'],
  ['deferred', '指定调用 · 分页正文'], ['unsupported', '未支持协议'],
  ['missing-evidence', '缺少证据'], ['slow', '迟到 A · 先切换 B'],
] as const
export type Mode = typeof choices[number][0]

export function receipt(attempt = ids.a, preparation = ids.preparation): TaskDetail {
  return {
    kind: 'preparation', id: preparation, category: 'production', title: `合成只读调用 · ${attempt}`,
    status: 'succeeded', operation: 'video.generate', project_id: ids.project, project_title: '合成项目',
    production_run_id: 'synthetic-run', execution_instance_version_id: null, execution_instance_name: null,
    execution_version: null, preparation_id: preparation, source_kind: 'generation_attempt', source_id: attempt,
    created_at: '2026-10-04T01:00:00Z', updated_at: '2026-10-04T01:00:00Z', finished_at: '2026-10-04T01:01:00Z',
    estimated_cost: null, actual_cost: null, cost_currency: '', anomaly: false, outcome: null,
    actions: { cancel_asset:false,cancel_smoke:false }, purpose: null, preparation_status: 'dispatched',
    request_frozen_at: '2026-10-04T01:00:00Z', request_sha256: 'a'.repeat(64),
    submission: { state: 'unavailable', started_at: null, registered_at: null, identifier_state: 'unavailable',
      message: '合成回执：提交证据不可用，不证明请求已发送或远端收到。', notes: [] },
    input: { state: 'frozen', submitted: { prompt: `合成输入 ${attempt}` }, resolved: {}, parameters: [],
      prompts: [{ key: 'prompt', role: 'user', text: `合成提示词 ${attempt}`, source: 'saved_request' }],
      prompt_trace: null, field_batch_trace: null, media_trace: null, api_mapping: null,
      native_input_trace: null, system_message_trace: null, notes: ['合成只读 API 回执，不是真实调用或生产验收。'] },
    output: { state: 'available', text: `合成输出 ${attempt}`, result: null, media: [], notes: [] },
    validation: { status: 'unavailable', warnings: [], error_code: '', error_message: '' },
    calls: [ids.a, ids.b].map(id => ({ id, kind: 'generation_attempt', role: 'primary', status: 'succeeded',
      operation: 'video.generate', preparation_id: preparation, retry_of_id: null,
      created_at: '2026-10-04T01:00:00Z', finished_at: '2026-10-04T01:01:00Z', error_code: '' })),
    call_count: 2, calls_next: 'page=2', calls_previous: null, output_count: 2, output_next: 'page=2', output_previous: null,
    deferred_fields: [], links: { generation_task_id: null, asset_job_id: null },
  }
}

export async function deferredReceipt(attempt = ids.a, page = 1, preparation = ids.preparation) {
  const value = receipt(attempt, preparation), body = `合成完整正文 ${attempt} · 产物页 ${page} 😀`, encoded = JSON.stringify(body)
  const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(encoded))))
    .map(byte => byte.toString(16).padStart(2, '0')).join('')
  value.output.text = ''
  value.deferred_fields = [{ field: 'output.text', sha256, size_bytes: new TextEncoder().encode(encoded).length,
    total_characters: Array.from(encoded).length, encoding: 'json' }]
  return { value, body, encoded, sha256 }
}

type Transport = {
  fetch?: typeof fetch
  XMLHttpRequest?: { prototype: Pick<XMLHttpRequest, 'send'> }
  navigator?: Pick<Navigator, 'sendBeacon'>
}
export function isolateHttp(respond: (id: string, query: Record<string, any>) => Promise<unknown>, transport: Transport = globalThis) {
  const requests = ref<{ id: string; query: Record<string, any> }[]>([]), blocked = ref(0)
  const block = (): never => { blocked.value++; throw Error('合成夹具禁止未声明 HTTP 或业务写入') }
  const oldGet = api.get, oldAdapter = api.defaults.adapter
  const oldFetch = transport.fetch, oldSend = transport.XMLHttpRequest?.prototype.send, oldBeacon = transport.navigator?.sendBeacon
  const interceptor = api.interceptors.request.use(() => block())
  api.defaults.adapter = async () => block()
  // Bypass authentication and transport only for the two declared fixture GETs.
  api.get = (async (url: string, config: any) => {
    const id = [ids.preparation, ids.other].find(id => url === `/task-center/preparation/${id}/`)
    const q = config?.params
    if (!id || !config || Object.keys(config).some(k => k !== 'params') || !q || typeof q !== 'object'
      || Object.keys(q).some(k => !['call_page', 'output_page', 'attempt_id', 'content_field', 'content_offset', 'content_limit'].includes(k))
      || ![1, 2].includes(q.call_page) || ![1, 2].includes(q.output_page)
      || (q.attempt_id !== undefined && ![ids.a, ids.b].includes(q.attempt_id))) return block()
    if ('content_field' in q || 'content_offset' in q || 'content_limit' in q) {
      if (q.content_field !== 'output.text' || !Number.isSafeInteger(q.content_offset) || q.content_offset < 0 || q.content_limit !== 65536) return block()
    }
    requests.value.push({ id, query: { ...q } })
    return { data: structuredClone(await respond(id, q)) }
  }) as typeof api.get
  if (oldFetch) transport.fetch = async () => block()
  if (oldSend) transport.XMLHttpRequest!.prototype.send = () => block()
  if (oldBeacon) transport.navigator!.sendBeacon = () => block()
  return { requests, blocked, restore() {
    api.get = oldGet; api.defaults.adapter = oldAdapter; api.interceptors.request.eject(interceptor)
    if (oldFetch) transport.fetch = oldFetch
    if (oldSend) transport.XMLHttpRequest!.prototype.send = oldSend
    if (oldBeacon) transport.navigator!.sendBeacon = oldBeacon
  } }
}

export function createFixture() {
  const mode = ref<Mode>('correct'), attempt = ref<string | undefined>(ids.a), preparation = ref(ids.preparation), revision = ref(0)
  const failNext = ref(false), pending = ref(false), events = ref<string[]>([]), navigation = ref('')
  let release: (() => void) | undefined
  const isolation = isolateHttp(async (id, query) => {
    const selectedMode = mode.value, selected = query.attempt_id || ids.b
    if (failNext.value) { failNext.value = false; throw Error('合成详情刷新失败：保留上次结果') }
    if (selectedMode === 'slow' && selected === ids.a) {
      pending.value = true
      await new Promise<void>(resolve => { release = resolve })
      pending.value = false
    }
    let value = receipt(selected, id)
    if (selectedMode === 'wrong-attempt') value = receipt(ids.b, id)
    if (selectedMode === 'wrong-kind') value.source_kind = 'asset_job'
    if (selectedMode === 'missing-source') value.source_id = null
    if (selectedMode === 'unsupported' || selectedMode === 'missing-evidence') {
      value.input.state = 'unavailable'; value.input.prompts = []
      value.output = { state: 'unavailable', text: '', result: null, media: [], notes: ['原输出证据未记录，不能推断成功。'] }
      value.input.notes = [selectedMode === 'unsupported' ? '该协议未支持逐字节传输证据，无法核对。' : '原调用缺少输入证据，不补造历史。']
    }
    if (selectedMode === 'deferred') {
      const content = await deferredReceipt(selected, query.output_page, id)
      value = content.value
      if (query.content_field) {
        const characters = Array.from(content.encoded), offset = query.content_offset, end = Math.min(offset + 17, characters.length)
        value.content = { field: 'output.text', offset, next_offset: end < characters.length ? end : null,
          text: characters.slice(offset, end).join(''), total_characters: characters.length, sha256: content.sha256 }
      }
    }
    value.calls = query.call_page === 1 ? value.calls : [{ ...value.calls[0]!, preparation_id: ids.other }]
    value.calls_next = query.call_page === 1 ? 'page=2' : null; value.calls_previous = query.call_page === 2 ? 'page=1' : null
    value.output_next = query.output_page === 1 ? 'page=2' : null; value.output_previous = query.output_page === 2 ? 'page=1' : null
    return value
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  function select(kind: TaskKind, id: string, selected?: string) {
    events.value.push(JSON.stringify([kind, id, selected ?? null]))
    if (kind !== 'preparation' || ![ids.preparation, ids.other].includes(id) || (selected && ![ids.a, ids.b].includes(selected))) throw Error('未声明的合成选择')
    preparation.value = id; attempt.value = selected
  }
  function interceptLink(event: MouseEvent) {
    const link = (event.target as Element | null)?.closest('a')
    if (!link) return
    event.preventDefault(); event.stopPropagation()
    navigation.value = `已截获只读跳转：${link.getAttribute('href')}`
  }
  const app = createApp({ setup: () => () => h('main', { class: 'fixture-shell',
    onClickCapture: interceptLink, onAuxclickCapture: interceptLink, onContextmenuCapture: interceptLink,
  }, [
    h('h1', 'M7-07 调用身份 · 合成只读回执'),
    h('p', '仅挂载真实 Vue 组件；所有回执来自内存。无业务写入、外网、凭据、真实模型或 Smoke。'),
    h('div', { class: 'fixture-choices' }, choices.map(([key, label]) => h('button', { class: 'button ghost',
      'aria-pressed': mode.value === key, onClick: () => { release?.(); mode.value = key; attempt.value = ids.a; preparation.value = ids.preparation; revision.value++ },
    }, label))),
    h('div', { class: 'fixture-choices' }, [
      h('button', { class: 'button ghost', onClick: () => select('preparation', ids.preparation, ids.b) }, '夹具切换 B'),
      h('button', { class: 'button ghost', disabled: !pending.value, onClick: () => release?.() }, '释放迟到 A'),
      h('button', { class: 'button ghost', onClick: () => { failNext.value = true } }, '令下一读取失败'),
    ]),
    h('p', { role: 'status' }, `合成 GET ${isolation.requests.value.length} · 已阻断 ${isolation.blocked.value} · 选择 ${attempt.value || '最新调用'}`),
    h(TaskCenterDetail, { key: revision.value, kind: 'preparation', id: preparation.value, attemptId: attempt.value,
      readonly: true, expectedProjectId: ids.project, onSelect: select }),
    h('pre', { class: 'fixture-log' }, `${navigation.value}\n选择事件：${events.value.join('\n')}\n合成请求：${JSON.stringify(isolation.requests.value, null, 2)}`),
  ]) })
  app.use(createPinia()); app.use(router)
  return { app, router, isolation, release: () => release?.() }
}

if (typeof window !== 'undefined') {
  // Never read or mutate any real session storage, even on a reused origin.
  Object.defineProperty(window, 'localStorage', { configurable: true, value: { getItem: () => null,
    setItem: () => { throw Error('合成夹具禁止保存凭据') }, removeItem: () => { throw Error('合成夹具禁止删除凭据') } } })
  const style = document.createElement('style'); style.textContent = fixtureStyles; document.head.append(style)
  const fixture = createFixture()
  void fixture.router.push('/fixture').then(() => fixture.app.mount('#app'))
  window.addEventListener('pagehide', () => { fixture.app.unmount(); fixture.release(); fixture.isolation.restore() }, { once: true })
}
