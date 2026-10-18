// Declared synthetic prompt-display receipts, shared with the Node contract tests.
// Counts and limits are receipt facts, not a second prompt compiler/normalizer.
import { createApp, h, ref } from 'vue'
import type { AxiosAdapter } from 'axios'
import ExecutionPromptDisplay from '../../src/components/ExecutionPromptDisplay.vue'
import { api } from '../../src/services/api'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

export const preparationId = '00000000-0000-4000-8000-000000000274'
export const profileVersionId = '00000000-0000-4000-8000-000000000701'
export const definitionVersionId = '00000000-0000-4000-8000-000000000801'
export const profileSha = 'b'.repeat(64)
export const directionText = '导演：' + '中😀'.repeat(1000)
export const soundText = '声音：' + '声'.repeat(2000)
export const positiveText = `${directionText}\n\n${soundText}\n\n参考：<Picture 1> 沈念`
export const fieldLabels = { custom_direction: '导演描述', custom_sound: '原声音', custom_references: '系统投影 · 参考关系' }
export const scenarios = [
  ['cumulative', '单字段合规 · 累计超限'], ['target', '目标接口更严格'],
  ['preview', '有效策略预览'], ['frozen', '同版最终冻结'],
  ['unlimited', '未配置通道上限'], ['manual', '人工冻结（无策略）'],
  ['auxiliary', '辅助文本冻结消息'], ['none', '无提示词非聊天请求'],
  ['unavailable', '来源缺失'], ['invalid', '非法计数拒绝'],
] as const
export type Scenario = typeof scenarios[number][0]
type Raw = Record<string, any>
export function promptReceipt(scenario: Scenario = 'preview'): Raw {
  const mode = scenario === 'cumulative' || scenario === 'target' ? 'diagnostic'
    : ['frozen', 'manual', 'auxiliary', 'none'].includes(scenario) ? 'frozen' : scenario === 'unavailable' ? 'unavailable' : 'preview'
  const row: Raw = {
    preparation_id: preparationId, preparation_revision: 8, mode,
    prompt_mode: scenario === 'manual' ? 'manual' : ['auxiliary', 'none'].includes(scenario) ? 'none' : 'profile',
    content_kind: mode === 'unavailable' ? 'unavailable' : 'channels', messages: [],
    request_sha256: mode === 'frozen' ? 'a'.repeat(64) : null,
    frozen_at: mode === 'frozen' ? '2026-10-04T00:00:00Z' : null,
    profile_basis: { version_id: profileVersionId, version: 7, contract_sha256: profileSha },
    issues: mode === 'diagnostic' ? ['通道 positive_prompt 装配后为 4027 字符，超过生效上限 4000'] : [],
    channels: [
      { key: 'positive_prompt', text: positiveText, character_count: 4027 },
      { key: 'negative_prompt', text: '排除：模糊', character_count: 5 },
      { key: 'system_instruction', text: '系统：保留原对白😀', character_count: 9 },
      { key: 'structured_input', text: '', character_count: 0 },
    ],
    length_diagnostics: [
      { channel: 'positive_prompt', character_count: 4027, max_chars: 5000, over_by: 0,
        limit_sources: [{ source: 'prompt_profile.channel_limits.positive_prompt', max_chars: 5000 },
          { source: 'frozen_target.input_schema.positive_prompt', max_chars: 6000 }],
        fragments: [
          { field_key: 'custom_direction', label: '导演描述', field_definition_version_id: definitionVersionId,
            source_selector: 'fixed.value', max_chars: 2100, text_chars: 2003, separator_chars: 0 },
          { field_key: 'custom_sound', label: '原声音', field_definition_version_id: '00000000-0000-4000-8000-000000000802',
            source_selector: 'projection.output', max_chars: 2100, text_chars: 2003, separator_chars: 2 },
          { field_key: 'custom_references', label: '系统投影 · 参考关系', field_definition_version_id: '00000000-0000-4000-8000-000000000803',
            source_selector: 'projection.output', max_chars: 100, text_chars: 17, separator_chars: 2 },
        ] },
      { channel: 'negative_prompt', character_count: 5, max_chars: 100, over_by: 0,
        limit_sources: [{ source: 'prompt_profile.channel_limits.negative_prompt', max_chars: 100 }],
        fragments: [{ field_key: 'custom_negative', label: '排除项', field_definition_version_id: '00000000-0000-4000-8000-000000000804',
          source_selector: 'fixed.value', max_chars: 100, text_chars: 5, separator_chars: 0 }] },
      { channel: 'system_instruction', character_count: 9, max_chars: null, over_by: 0,
        limit_sources: [{ source: 'frozen_target.input_schema.system_instruction', max_chars: null }],
        fragments: [{ field_key: 'custom_system', label: '系统段 · 原对白约束', field_definition_version_id: '00000000-0000-4000-8000-000000000805',
          source_selector: 'fixed.value', max_chars: 100, text_chars: 9, separator_chars: 0 }] },
      { channel: 'structured_input', character_count: 0, max_chars: null, over_by: 0, limit_sources: [], fragments: [] },
    ],
    field_basis: [{ unit_key: 'shot:mock-274', field_key: 'custom_direction',
      field_value_id: '00000000-0000-4000-8000-000000000901', revision: 1,
      origin: mode === 'frozen' ? 'adopted' : 'candidate', status: mode === 'frozen' ? 'confirmed' : 'pending' }],
    sources: { references: [{ label: '<Picture 1>', name: '沈念', category: 'character', guidance: '本次冻结的真实槽位（合成回执）' }],
      dialogue: [{ unit_key: 'shot:mock-274', subject: '沈念', text: '这是什么？<script>untrusted</script>😀',
        start_ms: 1093, end_ms: 2370, voiceover: false, delivery: 'softly' }] },
  }
  const positive = row.length_diagnostics[0]
  if (scenario === 'cumulative' || scenario === 'target') {
    positive.max_chars = 4000; positive.over_by = 27
    positive.limit_sources[scenario === 'cumulative' ? 0 : 1].max_chars = 4000
  }
  if (scenario === 'unlimited') {
    positive.max_chars = null
    positive.limit_sources.forEach((source: Raw) => { source.max_chars = null })
  }
  if (scenario === 'manual' || scenario === 'unavailable') {
    row.profile_basis = null; row.length_diagnostics = []; row.field_basis = []
    if (scenario === 'unavailable') { row.channels = []; row.issues = ['缺少本次冻结来源，不能改取当前策略。'] }
  }
  if (scenario === 'auxiliary' || scenario === 'none') {
    row.content_kind = scenario === 'auxiliary' ? 'messages' : 'not_applicable'
    row.channels = []; row.profile_basis = null; row.length_diagnostics = []; row.field_basis = []
    row.sources = { references: [], dialogue: [] }
    if (scenario === 'auxiliary') row.messages = [
      { role: 'system', content: '  Return only JSON with items and request_id.\nPreserve field rules.  ' },
      { role: 'user', content: ' {"operation":"prompt.fields.generate","items":[{"request_id":"field-1","source_text":"' + '<script>untrusted</script>中😀'.repeat(180) + '"}]}\n' },
    ].map(message => ({ ...message, character_count: Array.from(message.content).length }))
  }
  if (scenario === 'invalid') row.channels[0].character_count = 5027 // UTF-16 units, deliberately not code points.
  return row
}

export function installIsolation(getScenario: () => Scenario) {
  const reads = ref(0), blocked = ref(0), events = ref<unknown[]>([])
  function record(event: unknown) {
    events.value = [...events.value.slice(-79), event]
    window.dispatchEvent(new CustomEvent('prompt-length-fixture:http', { detail: event }))
  }
  const deny = (method: string, url: string): never => {
    blocked.value++; record({ method, url, blocked: true })
    throw new Error(`隔离夹具阻断未声明请求：${method} ${url}`)
  }
  const adapter: AxiosAdapter = async config => {
    const method = String(config.method).toUpperCase(), url = String(config.url)
    if (method !== 'GET' || url !== `/execution-preparations/${preparationId}/prompt-display/`
        || JSON.stringify(config.params) !== JSON.stringify({ expected_preparation_revision: 8 }) || config.data !== undefined) return deny(method, url)
    const data = promptReceipt(getScenario())
    reads.value++; record({ method, url, params: config.params, scenario: getScenario(), response: data })
    return { data, status: 200, statusText: 'OK', config, headers: {} }
  }
  // No real credentials are read; even per-request adapter overrides are blocked.
  api.interceptors.request.clear(); api.defaults.adapter = adapter
  api.interceptors.request.use(config => { config.adapter = adapter; config.headers.delete('Authorization'); return config })
  window.fetch = async input => deny('fetch', typeof input === 'string' ? input : input instanceof URL ? input.href : input.url)
  const requests = new WeakMap<XMLHttpRequest, [string, string]>()
  XMLHttpRequest.prototype.open = function (...args: any[]) { requests.set(this, [String(args[0]), String(args[1])]) }
  XMLHttpRequest.prototype.send = function () { const [method, url] = requests.get(this) || ['XHR', 'unknown']; deny(method, url) }
  navigator.sendBeacon = url => deny('sendBeacon', String(url))
  return { reads, blocked, events, adapter }
}

export function startFixture() {
  const scenario = ref<Scenario>('cumulative'), epoch = ref(0)
  const isolation = installIsolation(() => scenario.value)
  const style = document.createElement('style')
  style.textContent = baseStyles.replace(/@import\s+url\([^;]+;/g, '')
  document.head.append(style)
  createApp({ setup: () => () => h('main', { class: 'fixture-shell' }, [
    h('h1', 'M6-06 全文与长度诊断'),
    h('div', { class: 'fixture-warning', role: 'note' }, [h('strong', '隔离模拟 / MOCK · 零模型 · 零现用业务'),
      h('p', '真实展示组件，仅允许一条声明的只读 GET。无采用、冻结、提交或生成操作；fetch/XHR/beacon 及全部未声明业务请求均阻断。'),
      h('p', '两个字段正文各2000码点、含标签各2003，上限各2100；累计含系统参考段与分隔符为4027。请选择场景，再点击组件内“预览/查看”按钮。')]),
    h('nav', { class: 'fixture-controls', 'aria-label': '隔离场景' }, scenarios.map(([key, label]) =>
      h('button', { type: 'button', class: 'button ghost', 'aria-pressed': scenario.value === key,
        onClick: () => { scenario.value = key; epoch.value++ } }, label))),
    h(ExecutionPromptDisplay, { key: epoch.value, preparationId, revision: 8, disabled: false,
      frozen: ['frozen', 'manual', 'auxiliary', 'none'].includes(scenario.value), fieldLabels }),
    h('p', { role: 'status' }, `模拟 GET：${isolation.reads.value} · 业务写入：0 · 未声明/网络阻断：${isolation.blocked.value}`),
    h('details', { class: 'fixture-log' }, [h('summary', '确切请求与声明回执日志'), h('pre', JSON.stringify(isolation.events.value, null, 2))]),
  ]) }).mount('#app')
}
if (typeof document !== 'undefined') startFixture()
