// Isolated UI fixture. All records and digests are synthetic; no business API,
// model, approval or live configuration is used. Navigation stays in this page.
import { createApp, h, ref } from 'vue'
import ExecutionPreparationFacts from '../../src/components/ExecutionPreparationFacts.vue'
import { api } from '../../src/services/api'
import '../../src/styles.css'
import '../../src/platform.css'

const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
const selected = ref('ready'), reads = ref(0), blockedRequests = ref(0), navigation = ref(''), pending: (() => void)[] = []
const label = '冻结的动态描述 · 同名字段 / 多语言动作 Ａction'
function saved(alternate = false): Record<string, unknown> {
  const profile = id(alternate ? 11 : 1), definition = id(alternate ? 12 : 2), version = id(alternate ? 13 : 3)
  return { schema_version: 1, inputs: { logical_values: { action: '保持本次已保存输入' } }, parameters: { values: {} },
    profile_sources: { profile: { version_id: profile, contract_sha256: 'a'.repeat(64) } },
    field_requests: [{ schema_version: 1, size_bytes: 1024, sha256: 'b'.repeat(64), value: {
      schema_version: 1, operation: 'prompt.fields.generate', profile: { version_id: profile, contract_sha256: 'a'.repeat(64) },
      items: ['shot:first', 'shot:second'].map(unit => ({ unit_key: unit, field_contract: { field_key: 'custom_motion',
        field_definition: { schema_version: 1, field_definition_id: definition, field_definition_version_id: version,
          key: 'custom_motion', label: alternate ? '另一份准备的冻结字段' : label, version: 2 } } })),
    } }],
  }
}
const inputs = ref(saved())
async function readFixtureRelations(url: string, options: any = {}) {
  const params = options?.params
  const known = params?.kind === 'prompt' ? [id(1), id(11)] : params?.kind === 'field_version' ? [id(3), id(13)] : []
  const ids = typeof params?.ids === 'string' ? params.ids.split(',') : []
  if (url !== '/registry-relations/' || !options || typeof options !== 'object' || Array.isArray(options)
    || !params || typeof params !== 'object' || Array.isArray(params)
    || Object.keys(options).some(key => !['params', 'signal'].includes(key))
    || Object.keys(params).some(key => !['kind', 'ids', 'page_size', 'page'].includes(key))
    || !ids.length || ids.some(objectId => !known.includes(objectId)) || new Set(ids).size !== ids.length
    || !Number.isSafeInteger(params.page) || params.page < 1
    || !Number.isSafeInteger(params.page_size) || params.page_size < 1 || params.page_size > 50) {
    return blockHttp()
  }
  reads.value++
  const mode = selected.value
  if (mode === 'delayed') await new Promise<void>(resolve => pending.push(resolve)) // Deliberately ignores abort to test late responses.
  if (mode === 'forbidden' && params.kind === 'field_version') throw { response: { status: 403, data: { detail: '没有查看字段配置的权限（隔离示例）' } } }
  if (mode === 'failed') throw Error('关联目录读取失败（隔离示例，可重试）')
  const rows = mode === 'missing' ? [] : ids.map(objectId => ({
    id: objectId, kind: params.kind, label: '当前目录名称：不可覆盖冻结名称', status: 'retired', groups: [], notices: [], capabilities: [],
    config_url: params.kind === 'prompt' ? `/settings?section=prompts&prompt_profile_version=${objectId}`
      : `/settings?section=fields&field_definition=${objectId === id(13) ? id(12) : id(2)}&field_version=${mode === 'conflict' ? id(99) : objectId}`,
    overview_url: '/settings/acceptance-smoke?view=relations', acceptance_url: '',
  }))
  const start = (params.page - 1) * params.page_size
  return { data: { count: rows.length, results: rows.slice(start, start + params.page_size),
    next: start + params.page_size < rows.length ? '/registry-relations/' : null,
    previous: params.page > 1 ? '/registry-relations/' : null, generated_at: 'fixture', context_label: '' } }
}
function blockHttp(): never {
  blockedRequests.value++
  throw Error('隔离夹具已拦截未声明的业务 HTTP 请求；仅允许夹具目录 GET，不会发送到网络。')
}
type FixtureTransport = {
  fetch?: typeof fetch
  XMLHttpRequest?: { prototype: Pick<XMLHttpRequest, 'send'> }
  navigator?: Pick<Navigator, 'sendBeacon'>
}
// The request interceptor also blocks api(config), api.request and per-request
// adapter overrides, before the application's authentication interceptor runs.
// Raw browser transports are blocked separately. Only the declared in-memory
// GET above bypasses these guards; no live credentials or API are needed.
export function installFixtureHttpIsolation(client = api, transport: FixtureTransport = globalThis) {
  const oldGet = client.get, oldAdapter = client.defaults.adapter
  const oldFetch = transport.fetch, oldSend = transport.XMLHttpRequest?.prototype.send, oldBeacon = transport.navigator?.sendBeacon
  const interceptor = client.interceptors.request.use(() => blockHttp())
  client.defaults.adapter = async () => blockHttp()
  client.get = readFixtureRelations as typeof client.get
  if (oldFetch) transport.fetch = async () => blockHttp()
  if (oldSend) transport.XMLHttpRequest!.prototype.send = () => blockHttp()
  if (oldBeacon) transport.navigator!.sendBeacon = () => blockHttp()
  return { reads, blockedRequests, restore() {
    client.interceptors.request.eject(interceptor)
    client.get = oldGet; client.defaults.adapter = oldAdapter
    if (oldFetch) transport.fetch = oldFetch
    if (oldSend) transport.XMLHttpRequest!.prototype.send = oldSend
    if (oldBeacon) transport.navigator!.sendBeacon = oldBeacon
  } }
}

const choices = [['ready', '正常／同名字段'], ['forbidden', '字段配置无权限'], ['failed', '目录失败'], ['missing', '配置已不存在'],
  ['conflict', '目录目标矛盾'], ['empty', '明确无辅助批次'], ['invalid', '缺失冻结来源'], ['delayed', '延迟响应']] as const
function change(value: string) {
  selected.value = value; navigation.value = ''; inputs.value = saved()
  if (value === 'empty') inputs.value = { ...inputs.value, field_requests: [] }
  if (value === 'invalid') { const { field_requests: omitted, ...rest } = inputs.value; inputs.value = rest }
}
function interceptNavigation(event: MouseEvent) {
  const link = (event.target as Element).closest('a')
  if (link) { event.preventDefault(); navigation.value = link.getAttribute('href') || '' }
}
const app = createApp({ setup() { return () => h('main', {
  style: { maxWidth: '1000px', margin: '20px auto', padding: '12px', minWidth: '0' },
  onClickCapture: interceptNavigation, onAuxclickCapture: interceptNavigation, onContextmenuCapture: interceptNavigation,
}, [
  h('h1', '辅助字段配置来源 · 隔离验证'),
  h('p', '全部内容均为合成夹具。展开“输入与运行参数 → 辅助字段配置来源”后才读取模拟目录；没有模型调用或业务写入。'),
  h('label', { style: { display: 'grid', gap: '8px' } }, ['场景', h('select', { value: selected.value,
    style: { minHeight: '40px', maxWidth: '100%' }, onChange: (event: Event) => change((event.target as HTMLSelectElement).value) },
  choices.map(([value, name]) => h('option', { value }, name)))]),
  h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px', margin: '12px 0' } }, [
    h('button', { type: 'button', class: 'button', onClick: () => { selected.value = 'ready'; inputs.value = saved(true) } }, '切换到另一份准备'),
    h('button', { type: 'button', class: 'button', onClick: () => { for (const resolve of pending.splice(0)) resolve() } }, '释放旧响应'),
    h('button', { type: 'button', class: 'button', onClick: () => { selected.value = 'ready' } }, '恢复目录（随后点入口重试）'),
  ]),
  h('p', { role: 'status' }, `模拟目录读取 ${reads.value} 次；已拦截未声明请求 ${blockedRequests.value} 次；真实业务请求 0 次。`),
  h(ExecutionPreparationFacts, { resolvedInputs: inputs.value }),
  navigation.value ? h('p', { role: 'status', style: { overflowWrap: 'anywhere' } }, `已拦截配置跳转：${navigation.value}`) : null,
]) } })
if (typeof document !== 'undefined') {
  installFixtureHttpIsolation()
  app.mount('#app')
}
