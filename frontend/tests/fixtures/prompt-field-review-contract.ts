// Browser-safe mirror of declared receipts in promptFieldReviewContract.test.ts.
// No evaluator, model, database or alternative production review implementation runs.
import { createApp, h, ref } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import type { AxiosAdapter } from 'axios'
import StageExecutionPreparations from '../../src/components/StageExecutionPreparations.vue'
import ExecutionPreparationWorkbench from '../../src/components/ExecutionPreparationWorkbench.vue'
import { api } from '../../src/services/api'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

type Raw = Record<string, any>
type Snapshot = { preparation: Raw; fields: Raw[] }
type Mode = 'manual' | 'policy' | 'stale' | 'blocked'
type Case = { current: Snapshot; after: Snapshot; replaced?: Snapshot; stale?: Snapshot; source?: Snapshot }
export const choices = [['manual', '人工审核'], ['policy', '自动策略'], ['stale', '陈旧冲突'], ['blocked', '文件与映射硬错误']] as const
const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
export const stageId = id(1), versionId = id(2)
const stamp = '2026-10-04T00:00:00Z'
const clone = <T>(value: T): T => structuredClone(value)
const page = (results: unknown[]) => ({ count: results.length, next: null, previous: null, results })
const ordered = (value: any): any => Array.isArray(value) ? value.map(ordered)
  : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])])) : value
const same = (a: unknown, b: unknown) => JSON.stringify(ordered(a)) === JSON.stringify(ordered(b))
const definitions = [
  { key: 'm6_custom_direction', language: 'English', policy: 'inherit', text: '  The courier opens the parcel.\nThen pauses beside it.  ' },
  { key: 'm6_custom_sound', language: 'Chinese', policy: 'preserve_source', text: '原声：轻轻叩门。\n保留这句声音说明。' },
  { key: 'm6_custom_rhythm', language: 'any', policy: 'any', text: 'Beat one：停顿；beat two：转身。\n<script>not executable</script>' },
]
async function digest(value: string) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('')
}

async function records(number: number): Promise<Snapshot> {
  const preparationId = id(10 + number), unit = `shot:${id(30 + number)}`
  const fields = await Promise.all(definitions.map(async (definition, index) => ({
    id: id(100 * number + index), preparation: preparationId, unit_key: unit, field_key: definition.key, revision: 1,
    source_sha256: await digest(`source:${number}:${index}`), field_contract_sha256: await digest(`contract:${index}`), text_sha256: await digest(definition.text),
    origin_kind: 'manual', origin_attempt: null, origin_asset_job: null, status: 'pending', approval_kind: null,
    approval_label: '尚未审核', reviewed_by: null, reviewed_at: null, approval_policy_sha256: null,
    created_by: 1, created_at: stamp, updated_at: stamp,
    warnings: index === 0 ? [{ code: 'recommended_length_out_of_range', message: '建议至少100字；本夹具正文保留原值，不截断、不强制扩写。' }] : [], text: definition.text,
  })))
  return { fields, preparation: {
    id: preparationId, stage_run: stageId, asset_job: null, deployment_smoke: null, workflow_smoke: null,
    execution_instance_version: versionId, purpose: 'production', unit_key: unit, request_key: `candidate-${number}`, revision: 4,
    status: 'awaiting_review', intent_sha256: await digest(`intent:${number}`), request_sha256: null, request_frozen_at: null,
    next_check_at: null, reason_code: '', reason: '', evaluation_revision: null, evaluated_at: null, evaluation: null,
    cancellation: { allowed: false, reason: '隔离夹具不提供取消或执行操作。', cancelled_at: null, cancelled_by: null },
    presentation: { kind: 'primary', label: `第1集 · 镜头${number}`, operation: 'video.generate', parent: null, task: null },
    outcome: { message: '隔离模拟回执；零模型、零现用业务。', artifact_count: 0, selected_count: 0 },
    created_by: 1, created_at: stamp, updated_at: stamp, intent: {}, resolved_inputs: {},
    review_contract: { schema_version: 1, mode: 'manual' },
    review_summary: { mode: 'manual', label: '人工确认字段', description: '以下操作只切换内存中的预置回执，没有真实审核或调用。' },
    prepared_request: null, capacity_sample: null, response_diagnostic: null, resource_review: null, text_response_review: null,
    auxiliary_batches: [], field_selections: [],
    field_slots: definitions.map((definition, index) => ({
      unit_key: unit, field_key: definition.key, field_label: `Exact ${definition.key}`, next_revision: 2,
      latest_value_id: fields[index]!.id, source_language: 'Chinese', target_language: 'English', effective_language: definition.language,
      field_contract: { field_key: definition.key, field_definition: { field_definition_id: id(400 + index),
        field_definition_version_id: id(500 + index), key: definition.key, label: `Exact ${definition.key}`, version: 1 },
        generation_required: true, required: true, max_chars: 1000, generation_instruction: 'Use only declared source facts.',
        generation_mode: 'model_generated', language_policy: definition.policy, source_selector: 'request.text' },
    })),
  } }
}
function reviewedSnapshot(input: Snapshot, kind: 'human' | 'policy', policySha: string): Snapshot {
  const result = clone(input)
  result.preparation.revision = 8
  result.fields = result.fields.map(field => ({ ...field, status: 'confirmed', approval_kind: kind,
    approval_label: kind === 'human' ? '人工审核' : '自动策略批准（非人工审核）', reviewed_by: kind === 'human' ? 1 : null,
    reviewed_at: stamp, approval_policy_sha256: kind === 'human' ? null : policySha }))
  result.preparation.field_selections = result.fields.map(field => ({ id: `selection-${field.id}`, preparation: result.preparation.id,
    unit_key: field.unit_key, field_key: field.field_key, field_value: field.id, revision: 1, selected_by: 1, selected_at: stamp }))
  return result
}
async function addLater(input: Snapshot, number: number, text: string) {
  const newer = { ...clone(input.fields[0]!), id: id(number), revision: 2, text, text_sha256: await digest(text),
    status: 'pending', approval_kind: null, approval_label: '尚未审核', reviewed_by: null, reviewed_at: null, approval_policy_sha256: null }
  input.fields.push(newer); input.preparation.field_slots[0].latest_value_id = newer.id
  input.preparation.field_slots[0].next_revision = 3
  return newer
}

export async function fixtureRecords(mode: Mode) {
  const policySha = await digest('policy'), initial = await Promise.all([1, 2, 3].map(records))
  const cases: Case[] = initial.map(input => ({ current: input, after: reviewedSnapshot(input, 'human', policySha) }))
  const previews = new Map<Snapshot, Raw>()
  async function declarePreview(snapshot: Snapshot, states: Array<'candidate' | 'selected' | 'blocked'>, ready: boolean, reason = '') {
    const p = snapshot.preparation
    previews.set(snapshot, { preparation_id: p.id, expected_preparation_revision: p.revision,
      preview_sha256: await digest(`preview:${p.id}:${p.revision}`), label: p.presentation.label, ready, reason,
      fields: states.map((state, index) => {
        const field = snapshot.fields[index]!, chosen = p.field_selections.find((s: Raw) => s.field_key === field.field_key)
        return { unit_key: field.unit_key, field_key: field.field_key, label: `Exact ${field.field_key}`, state,
          reason: state === 'selected' ? '已采用，保持不变' : state === 'blocked' ? reason : '',
          value_id: state === 'candidate' ? field.id : state === 'selected' ? chosen.field_value : null,
          revision: state === 'candidate' ? field.revision : null, text: state === 'candidate' ? field.text : '',
          warnings: state === 'candidate' ? field.warnings.map((warning: Raw) => warning.message) : [] }
      }) })
  }
  const allSelected = (s: Snapshot) => declarePreview(s, ['selected', 'selected', 'selected'], false, '已采用原版本，无需再次确认；不追新候选。')
  // Mirror the mixed-batch receipt: v1 remains chosen while v2 is pending.
  if (mode === 'manual') {
    const mixed = cases[1]!, approved = mixed.after
    mixed.current.fields[0] = clone(approved.fields[0]!); mixed.current.preparation.revision = 8
    mixed.current.preparation.field_selections = [clone(approved.preparation.field_selections[0])]
    const newer = await addLater(mixed.current, 900, 'New unadopted body.')
    approved.preparation.revision = 10; approved.fields.push(clone(newer))
    approved.preparation.field_slots = clone(mixed.current.preparation.field_slots)
    const replaced = clone(approved), replacement = replaced.fields[3]!
    Object.assign(replacement, { status: 'confirmed', approval_kind: 'human', approval_label: '人工审核', reviewed_by: 1, reviewed_at: stamp })
    replaced.preparation.revision = 12
    Object.assign(replaced.preparation.field_selections[0], { field_value: replacement.id, revision: 2 })
    mixed.replaced = replaced
    await declarePreview(mixed.current, ['selected', 'candidate', 'candidate'], true)
    await allSelected(replaced)
  }
  if (mode === 'policy') {
    const contract = { schema_version: 1, mode: 'policy', policy: { key: 'fixture-policy', version: 1, implementation_sha256: policySha } }
    for (const entry of cases) for (const s of [entry.current, entry.after]) {
      s.preparation.review_contract = clone(contract)
      s.preparation.review_summary = { mode: 'policy', label: '自动策略批准', description: '预置自动策略回执，不要求逐字段人审；并未实际运行策略。' }
    }
    const automatic = reviewedSnapshot(cases[0]!.current, 'policy', policySha)
    automatic.preparation.status = 'preparing'
    await addLater(automatic, 902, 'Later policy candidate, not selected.')
    cases[0]!.current = automatic; cases[0]!.after = automatic
    await allSelected(automatic)
    const ambiguous = cases[1]!, first = ambiguous.current
    first.preparation.reason_code = 'field_selection_conflict'
    first.preparation.reason = '多个可选修订，不会自动选择最新版本。'
    const newer = await addLater(first, 901, 'Another pending candidate.')
    const resolved = reviewedSnapshot(initial[1]!, 'policy', policySha)
    // This declared subsequent GET is after policy evaluation, not a client evaluator.
    resolved.fields = resolved.fields.slice(0, 3); resolved.preparation.field_selections = resolved.preparation.field_selections.slice(0, 3)
    Object.assign(resolved.fields[0]!, { approval_kind: 'human', approval_label: '人工审核', reviewed_by: 1, approval_policy_sha256: null })
    resolved.fields.push(clone(newer)); resolved.preparation.reason = ''; resolved.preparation.reason_code = ''; resolved.preparation.status = 'preparing'
    ambiguous.after = resolved
    await declarePreview(first, ['blocked', 'candidate', 'candidate'], false, first.preparation.reason)
  }
  // Cross-preparation reuse keeps source value IDs and has no local value history.
  const source = reviewedSnapshot(initial[2]!, mode === 'policy' ? 'policy' : 'human', policySha), reused = clone(source)
  reused.preparation.id = id(800); reused.preparation.request_key = 'candidate-2'
  reused.preparation.field_selections.forEach((s: Raw) => { s.preparation = reused.preparation.id })
  cases[2] = { current: reused, after: reused, source }
  await allSelected(reused)
  if (mode === 'stale') {
    const changed = clone(cases[1]!.current)
    changed.preparation.revision = 5; changed.preparation.reason = '多个可选修订；不会自动选择最新版本。'
    await addLater(changed, 901, 'A different pending candidate.')
    cases[1]!.stale = changed
    await declarePreview(changed, ['blocked', 'candidate', 'candidate'], false, changed.preparation.reason)
  }
  if (mode === 'blocked') {
    for (const [index, entry] of cases.entries()) {
      const reason = index === 0 ? '原参考文件缺失，不能准备请求' : '映射缺少必需目标字段'
      entry.current.preparation.status = 'blocked'
      entry.current.preparation.reason = reason
      await declarePreview(entry.current, ['blocked', 'blocked', 'blocked'], false, reason)
    }
  }
  for (const entry of cases) {
    if (!previews.has(entry.current)) await declarePreview(entry.current, ['candidate', 'candidate', 'candidate'], true)
    if (!previews.has(entry.after)) await allSelected(entry.after)
  }
  return { cases, previews }
}

export function installIsolation(getState: () => { mode: Mode; data: Awaited<ReturnType<typeof fixtureRecords>> }) {
  const reads = ref(0), writes = ref(0), blocked = ref(0), events = ref<Raw[]>([])
  function record(event: Raw) {
    const row = { sequence: reads.value + writes.value + blocked.value, ...event }
    events.value = [...events.value.slice(-99), row]
    window.dispatchEvent(new CustomEvent('prompt-field-review-fixture:http', { detail: clone(row) }))
  }
  function deny(method: string, url: string, payload?: unknown): never {
    blocked.value++; record({ kind: 'BLOCKED', method, url, payload })
    throw Error(`隔离夹具阻断未声明请求：${method} ${url}。未访问现用业务。`)
  }
  function conflict(message: string): never {
    throw Object.assign(Error('HTTP 409'), { response: { status: 409, headers: { 'content-type': 'application/json' },
      data: { error: { code: 'execution_preparation_conflict', details: { detail: [message] } } } } })
  }
  const adapter: AxiosAdapter = async config => {
    const { mode, data } = getState(), method = (config.method || '').toUpperCase(), url = config.url || ''
    let payload: any
    try { payload = typeof config.data === 'string' ? JSON.parse(config.data) : config.data } catch { return deny(method, url, 'invalid JSON') }
    const params = config.params ?? {}, entry = data.cases.find(item => url === `/execution-preparations/${item.current.preparation.id}/`)
    const respond = (body: unknown, kind = 'READ') => {
      if (kind === 'READ') reads.value++; else writes.value++
      record({ kind, method, url, params: clone(params), payload, response: clone(body) })
      return { data: clone(body), status: 200, statusText: 'OK', headers: {}, config }
    }
    const noParams = Object.keys(params).length === 0
    if (method === 'GET' && payload === undefined) {
      if (entry && noParams) return respond(entry.current.preparation)
      const source = data.cases.find(item => item.source && url === `/execution-preparations/${item.source.preparation.id}/`)
      if (source && noParams) return respond(source.source!.preparation)
      if (url === '/execution-preparations/groups/' && same(params, { stage_run_id: stageId, page: 1, kind: params.kind })
          && ['primary', 'auxiliary'].includes(params.kind)) return respond(page(params.kind === 'auxiliary' ? [] : data.cases.map(item => ({
        unit_key: item.current.preparation.unit_key, label: item.current.preparation.presentation.label,
        version_count: item.source ? 2 : 1, latest: item.current.preparation,
      }))))
      const history = data.cases.find(item => item.current.preparation.unit_key === params.unit_key)
      if (url === '/execution-preparations/' && history && same(params, { stage_run_id: stageId, page: 1, kind: 'primary', unit_key: params.unit_key })) {
        return respond(page([history.current.preparation, ...(history.source ? [history.source.preparation] : [])]))
      }
      const local = data.cases.flatMap(item => [item.current, ...(item.source ? [item.source] : [])]).find(item => item.preparation.id === params.preparation_id)
      if (url === '/execution-field-values/' && local && same(params, { preparation_id: params.preparation_id, page: 1 })) {
        return respond(page(local.fields.filter(field => field.preparation === params.preparation_id).slice().sort((a, b) => b.revision - a.revision)))
      }
      const field = data.cases.flatMap(item => item.current.fields).find(field => url === `/execution-field-values/${field.id}/`)
      if (field && noParams) return respond(field)
    }
    if (method === 'POST' && noParams && url === '/execution-preparations/bulk-field-preview/' && Array.isArray(payload?.preparation_ids)
        && same(Object.keys(payload), ['preparation_ids']) && payload.preparation_ids.length > 0 && new Set(payload.preparation_ids).size === payload.preparation_ids.length) {
      const listed = payload.preparation_ids.map((value: string) => data.cases.find(item => item.current.preparation.id === value))
      if (listed.every(Boolean)) return respond({ items: listed.map((item: Case) => data.previews.get(item.current)) })
    }
    if (method === 'POST' && noParams && url === '/execution-preparations/bulk-field-confirm/' && Array.isArray(payload?.items)
        && same(Object.keys(payload), ['items']) && payload.items.length > 0 && new Set(payload.items.map((r: Raw) => r.preparation_id)).size === payload.items.length) {
      const listed: Case[] = payload.items.map((r: Raw) => data.cases.find(item => item.current.preparation.id === r.preparation_id))
      if (!listed.every(Boolean)) return deny(method, url, payload)
      const rows = listed.map(item => data.previews.get(item.current)!)
      if (!rows.every((row, index) => row.ready && same(payload.items[index], { preparation_id: row.preparation_id,
        expected_preparation_revision: row.expected_preparation_revision, preview_sha256: row.preview_sha256 }))) return deny(method, url, payload)
      if (mode === 'stale') {
        writes.value++; record({ kind: 'MOCK 409 · NO ADOPTION', method, url, payload })
        const changed = data.cases[1]!; changed.current = changed.stale!
        return conflict('准备修订已更新；本批未采用任何字段。请重新核对确切版本。')
      }
      const count = rows.reduce((n, row) => n + row.fields.filter((field: Raw) => field.state === 'candidate').length, 0)
      listed.forEach(item => { item.current = item.after })
      return respond({ confirmed_field_count: count, items: listed.map(item => ({ preparation_id: item.current.preparation.id, revision: item.current.preparation.revision })) }, 'MOCK ADOPTION')
    }
    if (method === 'POST' && noParams && /\/execution-field-values\/[^/]+\/confirm\/$/.test(url)) {
      const candidate = data.cases[1]!, current = candidate.current
      const field = current.fields.find(f => url === `/execution-field-values/${f.id}/confirm/`)
      const replacing = mode === 'manual' && current === candidate.after && field?.id === id(900)
      const choosingOld = mode === 'policy' && current !== candidate.after && field?.id === id(200)
      if (field && (replacing || choosingOld) && same(payload, { expected_preparation_revision: current.preparation.revision,
        expected_text_sha256: field.text_sha256, expected_source_sha256: field.source_sha256,
        select_for_execution: true, replace: replacing, expected_selection_revision: replacing ? 1 : null })) {
        candidate.current = replacing ? candidate.replaced! : candidate.after
        return respond({ field: candidate.current.fields.find(f => f.id === field.id), preparation: candidate.current.preparation,
          selection: candidate.current.preparation.field_selections[0] }, 'MOCK EXPLICIT VERSION')
      }
    }
    // Includes continue/advance, generation, cancellation, create/edit/reject,
    // arbitrary GET, auth, uploads and adapters overridden by a caller.
    return deny(method, url, payload)
  }
  // Do not read real localStorage credentials. This instance belongs to this
  // standalone fixture document only; product source files are untouched.
  api.interceptors.request.clear()
  api.defaults.adapter = adapter
  api.interceptors.request.use(config => { config.adapter = adapter; config.headers.delete('Authorization'); return config })
  window.fetch = async (input) => deny('fetch', typeof input === 'string' ? input : input instanceof URL ? input.href : input.url)
  const xhrRequests = new WeakMap<XMLHttpRequest, { method: string; url: string }>(), originalOpen = XMLHttpRequest.prototype.open
  XMLHttpRequest.prototype.open = function (...args: any[]) {
    xhrRequests.set(this, { method: String(args[0]), url: String(args[1]) })
    return Reflect.apply(originalOpen, this, args)
  }
  XMLHttpRequest.prototype.send = function () { const request = xhrRequests.get(this); deny(request?.method || 'XHR', request?.url || 'XMLHttpRequest.send') }
  navigator.sendBeacon = url => deny('sendBeacon', String(url))
  return { reads, writes, blocked, events, adapter }
}

export async function startFixture() {
  const mode = ref<Mode>('manual'), view = ref<'single' | 'batch'>('single'), selected = ref(0), epoch = ref(0)
  const busy = ref(false), changing = ref(false), refresh = ref(0), fatal = ref('')
  let data = await fixtureRecords(mode.value)
  const isolation = installIsolation(() => ({ mode: mode.value, data }))
  async function choose(next: Mode) {
    if (busy.value || changing.value) return
    changing.value = true
    try { data = await fixtureRecords(next); mode.value = next; selected.value = 0; epoch.value++; refresh.value++ }
    catch (cause) { fatal.value = String(cause) }
    finally { changing.value = false }
  }
  const styles = baseStyles.replace(/@import\s*(?:url\([^)]*\)|"[^"]*"|'[^']*')\s*;/g, '')
  const Content = { setup: () => () => h('main', { class: 'fixture-shell', onClickCapture: (event: MouseEvent) => {
    if ((event.target as Element | null)?.closest('a')) { event.preventDefault(); event.stopPropagation() }
  }, onAuxclickCapture: (event: MouseEvent) => { event.preventDefault() } }, [
    h('style', styles),
    h('section', { class: 'fixture-warning', role: 'status' }, [h('h1', 'M6-05 / M7-06 字段审核 · 隔离模拟'),
      h('strong', '零模型 · 零现用业务 · 零真实网络写入'),
      h('p', '仅回放字段审核契约的合成API回执。采用按钮只切换内存夹具，不是实际生产成功；刷新页面即重置。禁止继续执行、生成、取消及未声明请求。')]),
    h('nav', { class: 'fixture-controls', 'aria-label': '隔离场景' }, [
      ...choices.map(([value, label]) => h('button', { type: 'button', class: 'button ghost', disabled: busy.value || changing.value,
        'aria-pressed': mode.value === value, onClick: () => choose(value) }, label)),
      h('button', { type: 'button', class: 'button ghost', disabled: busy.value || changing.value, onClick: () => choose(mode.value) }, '重置当前夹具'),
    ]),
    h('p', mode.value === 'manual'
      ? '镜1：三字段待审，可单镜一键。镜2：v1已选、v2待选；先批审剩余两字段，再可明确替换v2。镜3：跨准备复用原值，只读。批审全选预览后实际采用5个待审字段。'
      : mode.value === 'policy'
        ? '镜1：自动策略已选v1，新v2不替换。镜2：两个候选有歧义；展开修订列表选v1并明确采用，后续GET回放其他字段的策略批准。镜3：复用策略批准的原值。'
        : mode.value === 'blocked' ? '原文件或映射硬错误：批量预览禁止采用；不会伪造文件、修复映射或发送模型请求。'
          : '单镜或批审提交均回放409原文，本批零采用。重新读取将显示镜2有多个候选；切回人工或重置夹具可重新开始。'),
    h('p', '切换场景、视图或镜头会丢弃未保存的夹具输入。仅声明正文、版本历史、单镜一键、批审及上述确切版本选择；其他控件的请求将被阻断并计数。'),
    h('nav', { class: 'fixture-controls', 'aria-label': '验证入口' }, ['single', 'batch'].map(value => h('button', {
      type: 'button', class: 'button ghost', disabled: busy.value || changing.value, 'aria-pressed': view.value === value,
      onClick: () => { view.value = value as 'single' | 'batch'; epoch.value++ },
    }, value === 'single' ? '单镜真实工作台' : '多镜真实阶段列表'))),
    h('p', { role: 'status', 'data-testid': 'fixture-http-counts' }, `模拟读取/预览 ${isolation.reads.value}；模拟提交尝试 ${isolation.writes.value}；未声明请求已阻断 ${isolation.blocked.value}。真实网络业务请求始终为0。`),
    fatal.value ? h('p', { role: 'alert' }, fatal.value) : null,
    changing.value ? h('p', '正在重建隔离回执…') : view.value === 'single'
      ? h('section', [
        h('label', { class: 'fixture-select' }, ['选择夹具镜头', h('select', { value: selected.value, disabled: busy.value,
          onChange: (event: Event) => { selected.value = Number((event.target as HTMLSelectElement).value); epoch.value++ },
        }, data.cases.map((entry, index) => h('option', { value: index }, entry.current.preparation.presentation.label)))]),
        h(ExecutionPreparationWorkbench, { key: `single:${epoch.value}`, preparationId: data.cases[selected.value]!.current.preparation.id,
          versionId, purpose: 'production', refreshToken: String(refresh.value), onBusy: (value: boolean) => { busy.value = value }, onChanged: () => { refresh.value++ } }),
      ])
      : h(StageExecutionPreparations, { key: `batch:${epoch.value}`, stageId, onBusy: (value: boolean) => { busy.value = value }, onChanged: () => { refresh.value++ } }),
    h('details', { class: 'fixture-log', open: true }, [h('summary', '精确请求/回执与阻断日志（最近100条）'),
      h('p', '也会派发 DOM 事件 prompt-field-review-fixture:http；日志仅有合成身份/正文，没有凭据。'),
      h('pre', { 'data-testid': 'fixture-http-log' }, JSON.stringify(isolation.events.value, null, 2)),
    ]),
  ]) }
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'fixture', component: Content }] })
  const app = createApp({ render: () => h(RouterView) }); app.use(router)
  app.config.errorHandler = error => { fatal.value = String(error) }
  await router.push('/'); await router.isReady(); app.mount('#app')
  return { app, router, isolation }
}
if (typeof document !== 'undefined') void startFixture().catch(error => {
  const root = document.getElementById('app')
  if (root) root.textContent = `隔离夹具初始化失败，未启动任何业务操作：${String(error)}`
})
