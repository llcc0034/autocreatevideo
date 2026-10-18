import assert from 'node:assert/strict'
import test from 'node:test'
import { createHash } from 'node:crypto'
import http from 'node:http'
import https from 'node:https'
import net from 'node:net'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'
import { renderToString } from 'vue/server-renderer'

// Page-side counterparts of backend/studio/tests/test_prompt_field_review_contract.py
// (batch 271, seven contracts). These are declared API receipts, not a second
// implementation of review, CAS, policy evaluation or server-side selection.
// Real Vue setup/watch/handlers/templates and HTTP payload/response adapters run.
const root = fileURLToPath(new URL('..', import.meta.url))
const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
const stageId = id(1), versionId = id(2), stamp = '2026-10-04T00:00:00Z'
const digest = (value: string) => createHash('sha256').update(value).digest('hex')
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
const page = (results: unknown[]) => ({ count: results.length, next: null, previous: null, results })
const flush = async () => { for (let n = 0; n < 4; n++) { await nextTick(); await new Promise(resolve => setImmediate(resolve)) } }
const escaped = (value: string) => value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const definitions = [
  { key: 'm6_custom_direction', language: 'English', policy: 'inherit', text: '  The courier opens the parcel.\nThen pauses beside it.  ' },
  { key: 'm6_custom_sound', language: 'Chinese', policy: 'preserve_source', text: '原声：轻轻叩门。\n保留这句声音说明。' },
  { key: 'm6_custom_rhythm', language: 'any', policy: 'any', text: 'Beat one：停顿；beat two：转身。\n<script>not executable</script>' },
]
const policy = { key: 'fixture-policy', version: 1, implementation_sha256: digest('policy') }

function records(number = 1) {
  const preparationId = id(10 + number), unit = `shot:${id(30 + number)}`
  const fields = definitions.map((definition, index) => ({
    id: id(100 * number + index), preparation: preparationId, unit_key: unit, field_key: definition.key, revision: 1,
    source_sha256: digest(`source:${number}:${index}`), field_contract_sha256: digest(`contract:${index}`), text_sha256: digest(definition.text),
    origin_kind: 'manual', origin_attempt: null, origin_asset_job: null, status: 'pending', approval_kind: null,
    approval_label: '尚未审核', reviewed_by: null, reviewed_at: null, approval_policy_sha256: null,
    created_by: 1, created_at: stamp, updated_at: stamp, warnings: [], text: definition.text,
  }))
  const preparation: any = {
    id: preparationId, stage_run: stageId, asset_job: null, deployment_smoke: null, workflow_smoke: null,
    execution_instance_version: versionId, purpose: 'production', unit_key: unit, request_key: `candidate-${number}`, revision: 4,
    status: 'awaiting_review', intent_sha256: digest(`intent:${number}`), request_sha256: null, request_frozen_at: null,
    next_check_at: null, reason_code: '', reason: '', evaluation_revision: null, evaluated_at: null, evaluation: null,
    cancellation: { allowed: true, reason: '', cancelled_at: null, cancelled_by: null },
    presentation: { kind: 'primary', label: `第1集 · 镜头${number}`, operation: 'video.generate', parent: null, task: null },
    outcome: { message: '明确人工夹具；主调用尚未执行。', artifact_count: 0, selected_count: 0 },
    created_by: 1, created_at: stamp, updated_at: stamp, intent: {}, resolved_inputs: {},
    review_contract: { schema_version: 1, mode: 'manual' },
    review_summary: { mode: 'manual', label: '人工确认字段', description: '确认与采用分别保存；没有模型调用。' },
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
  }
  return { preparation, fields: fields as any[] }
}
function selection(preparation: any, field: any, revision = 1) {
  return { id: `selection-${field.id}`, preparation: preparation.id, unit_key: field.unit_key,
    field_key: field.field_key, field_value: field.id, revision, selected_by: 1, selected_at: stamp }
}
function reviewedSnapshot(input: ReturnType<typeof records>, kind = 'human') {
  const result = clone(input)
  result.preparation.revision = 8
  result.fields = result.fields.map(field => ({ ...field, status: 'confirmed', approval_kind: kind,
    approval_label: kind === 'human' ? '人工审核' : '自动策略批准（非人工审核）', reviewed_by: kind === 'human' ? 1 : null,
    reviewed_at: stamp, approval_policy_sha256: kind === 'human' ? null : digest('policy') }))
  // Selection actor is present even for policy approval; only reviewed_by is null.
  result.preparation.field_selections = result.fields.map(field => selection(result.preparation, field))
  return result
}
function preview(input: ReturnType<typeof records>) {
  return { preparation_id: input.preparation.id, expected_preparation_revision: input.preparation.revision,
    preview_sha256: digest(`preview:${input.preparation.id}:${input.preparation.revision}`), label: input.preparation.presentation.label,
    ready: true, reason: '', fields: input.fields.map(field => ({ unit_key: field.unit_key, field_key: field.field_key,
      label: `Exact ${field.field_key}`, state: 'candidate', reason: '', value_id: field.id, revision: field.revision, text: field.text, warnings: [] as string[] })) }
}
const receipt = (row: ReturnType<typeof preview>) => ({ preparation_id: row.preparation_id,
  expected_preparation_revision: row.expected_preparation_revision, preview_sha256: row.preview_sha256 })
const conflict = (message: string) => {
  throw Object.assign(Error('HTTP 409'), { response: { status: 409, headers: { 'content-type': 'application/json' },
    data: { error: { code: 'execution_preparation_conflict', details: { detail: [message] } } } } })
}

async function harness(t: any) {
  const vite = await createServer({ root, envFile: false, logLevel: 'silent', appType: 'custom',
    server: { middlewareMode: true, ws: false, watch: null }, optimizeDeps: { noDiscovery: true } })
  const previous = { localStorage: globalThis.localStorage, document: globalThis.document, HTMLElement: globalThis.HTMLElement }
  Object.assign(globalThis, { localStorage: { getItem: () => null }, document: { activeElement: null }, HTMLElement: class {} })
  const apps: any[] = [], calls: any[] = [], unexpected: string[] = []
  const handlers = new Map<string, (payload: any, config: any) => unknown>()
  const block = (label: string): never => { unexpected.push(label); throw Error(`Undeclared HTTP blocked: ${label}`) }
  t.mock.method(globalThis, 'fetch', async () => block('fetch'))
  for (const [name, transport] of [['http', http], ['https', https]] as const) {
    t.mock.method(transport, 'request', () => block(`${name}.request`))
    t.mock.method(transport, 'get', () => block(`${name}.get`))
  }
  t.mock.method(net.Socket.prototype, 'connect', () => block('socket.connect'))
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const originalAdapter = api.defaults.adapter
  api.defaults.adapter = async (config: any) => {
    const key = `${config.method} ${config.url}`, handler = handlers.get(key)
    if (!handler) return block(key)
    const payload = typeof config.data === 'string' ? JSON.parse(config.data) : config.data
    calls.push({ key, payload: payload === undefined ? undefined : clone(payload), params: clone(config.params ?? {}) })
    return { data: clone(await handler(payload, config)), status: 200, statusText: 'OK', headers: {}, config }
  }
  t.after(async () => {
    for (const app of apps.reverse()) app.unmount()
    await flush(); api.defaults.adapter = originalAdapter; await vite.close(); Object.assign(globalThis, previous)
    assert.deepEqual(unexpected, [], 'including caught errors: no undeclared reads, writes or model requests')
  })
  async function mount(name: string, props: any, emit: (...args: any[]) => void = () => {}) {
    const Component = (await vite.ssrLoadModule(`/src/components/${name}.vue`)).default
    let state: any
    const host = { setup() { state = Component.setup(props, { expose() {}, emit }); return () => h('div') } }
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: host }] })
    await router.push('/'); await router.isReady()
    const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
      insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
    const app = renderer.createApp({ render: () => h(RouterView) }); app.use(router)
    app.provide(ssrContextKey, { modules: new Set() }); apps.push(app); app.mount({}); await flush()
    return { state, props, app, render: async () => {
      const context: any = {}, view = createSSRApp({ render: () => h({ ...Component, setup: () => state }, props) }).use(router)
      const html = await renderToString(view, context)
      return html + Object.values(context.teleports ?? {}).join('')
    } }
  }
  function serve(input: ReturnType<typeof records>) {
    handlers.set(`get /execution-preparations/${input.preparation.id}/`, () => input.preparation)
    for (const field of input.fields) handlers.set(`get /execution-field-values/${field.id}/`, () => field)
  }
  const workbench = (input: ReturnType<typeof records>) => mount('ExecutionPreparationWorkbench', reactive({
    preparationId: input.preparation.id, versionId, purpose: 'production' }))
  const writes = () => calls.filter(call => call.key.startsWith('post ') && !call.key.endsWith('/bulk-field-preview/'))
  return { handlers, calls, mount, serve, workbench, writes }
}

test('M7 quality notices keep the complete field and allow only explicit manual adoption', async t => {
  const env = await harness(t), initial = records(), after = reviewedSnapshot(initial)
  const notices = [{ code: 'recommended_length_out_of_range', message: '建议至少100字；当前正文不截断。' },
    { code: 'generated_reference_labels', message: '建议调整文风，<script>不是可执行内容</script>。' }]
  initial.fields[0]!.warnings = notices; after.fields[0]!.warnings = notices
  after.fields = [after.fields[0], ...initial.fields.slice(1)]
  after.preparation.field_selections = after.preparation.field_selections.slice(0, 1)
  env.serve(initial)
  const wb = await env.workbench(initial), field = initial.fields[0]!
  await wb.state.openSlot(wb.state.preparation.value.fieldSlots[0])
  const html = await wb.render()
  assert.match(html, /内容提醒（不阻断采用）/)
  assert.ok(html.includes(escaped(field.text))); assert.ok(html.includes(escaped(notices[1]!.message)))
  assert.doesNotMatch(html, /<script>不是可执行内容/)
  assert.equal(wb.state.editable.value, true); assert.equal(env.writes().length, 0)
  assert.deepEqual(wb.state.preparation.value.fieldSelections, [])
  env.handlers.set(`post /execution-field-values/${field.id}/confirm/`, payload => {
    assert.equal(payload.select_for_execution, true); assert.equal(payload.expected_text_sha256, field.text_sha256)
    env.serve(after); return { field: after.fields[0], preparation: after.preparation,
      selection: after.preparation.field_selections[0] }
  })
  await wb.state.review('select'); await flush()
  assert.equal(env.writes().length, 1); assert.equal(wb.state.error.value, '')
  assert.equal(wb.state.field.value.text, field.text)
  assert.match(await wb.render(), /此修订已明确选入本次请求/)
  assert.match(await wb.render(), /内容提醒（不阻断采用）/)
})

test('M7 hard field rejection preserves the draft and original selection without retry or success', async t => {
  const env = await harness(t), input = reviewedSnapshot(records()), reason = '结果超过字段硬字符上限'
  env.serve(input)
  const wb = await env.workbench(input)
  await wb.state.openSlot(wb.state.preparation.value.fieldSlots[0]); wb.state.editField()
  const draft = 'a'.repeat(1001); wb.state.text.value = draft
  env.handlers.set('post /execution-field-values/', payload => {
    assert.equal(payload.text, draft)
    throw Object.assign(Error('HTTP 400'), { response: { status: 400, headers: {}, data: {
      error: { code: 'validation_error', details: { text: [reason] } },
    } } })
  })
  await wb.state.createField()
  assert.match(await wb.render(), new RegExp(reason)); assert.equal(wb.state.text.value, draft)
  assert.equal(wb.state.field.value.id, input.fields[0]!.id)
  assert.equal(wb.state.preparation.value.fieldSelections[0].fieldValue, input.fields[0]!.id)
  assert.equal(wb.state.needsRefresh.value, true); assert.equal(wb.state.notice.value, '')
  await wb.state.createField(); assert.equal(env.writes().length, 1)
})

test('M7 bulk review separates nonblocking notices from missing-file and mapping blockers', async t => {
  const env = await harness(t), input = records(), row = preview(input)
  row.fields[0]!.warnings = ['建议长度提醒']
  env.handlers.set('post /execution-preparations/bulk-field-preview/', () => ({ items: [row] }))
  const panel = await env.mount('ExecutionFieldBulkReview', { preparationIds: [input.preparation.id] })
  assert.match(await panel.render(), /内容提醒（不阻断采用）/)
  assert.equal(panel.state.fieldCount.value, 3); assert.equal(env.writes().length, 0)
  for (const reason of ['原参考文件缺失，不能准备请求', '映射缺少必需目标字段']) {
    const blocked = clone(row); blocked.ready = false; blocked.reason = reason
    blocked.fields = blocked.fields.map(field => ({ ...field, state: 'blocked', reason, value_id: null,
      revision: null, text: '', warnings: [] })) as typeof row.fields
    env.handlers.set('post /execution-preparations/bulk-field-preview/', () => ({ items: [blocked] }))
    await panel.state.load(); panel.state.acknowledged.value = true
    assert.match(await panel.render(), new RegExp(reason)); assert.equal(panel.state.fieldCount.value, 0)
    await panel.state.confirm(); assert.equal(env.writes().length, 0)
  }
})

test('single shot: actual workbench and bulk handler confirm only the three displayed custom versions', async t => {
  const env = await harness(t), initial = records(), after = reviewedSnapshot(initial), row = preview(initial)
  env.serve(initial)
  env.handlers.set('post /execution-preparations/bulk-field-preview/', payload => {
    assert.deepEqual(payload, { preparation_ids: [initial.preparation.id] }); return { items: [row] }
  })
  env.handlers.set('post /execution-preparations/bulk-field-confirm/', payload => {
    assert.deepEqual(payload, { items: [receipt(row)] }); env.serve(after)
    return { confirmed_field_count: 3, items: [{ preparation_id: after.preparation.id, revision: 8 }] }
  })
  const wb = await env.workbench(initial)
  assert.equal(wb.state.error.value, ''); assert.match(await wb.render(), /本镜字段一键确认并采用/)
  for (const [index, slot] of wb.state.preparation.value.fieldSlots.entries()) {
    await wb.state.openSlot(slot)
    assert.equal(wb.state.field.value.text, definitions[index]!.text)
    assert.equal(slot.effectiveLanguage, definitions[index]!.language)
    assert.equal(slot.fieldContract.field_definition.version, 1)
    assert.equal(slot.fieldContract.field_definition.field_definition_version_id, id(500 + index))
    const html = await wb.render()
    assert.ok(html.includes(escaped(definitions[index]!.text)), 'actual full body, not a reconstructed summary')
    assert.ok(html.includes(`实际输出语言：${['english', 'chinese', '不限语言'][index]}`))
    assert.match(html, /修订 1/); assert.doesNotMatch(html, /<script>not executable/)
  }
  const pending: Promise<unknown>[] = []
  const panel = await env.mount('ExecutionFieldBulkReview', { preparationIds: [initial.preparation.id] }, (event, value) => {
    if (event === 'busy') wb.state.busy.value = value
    if (event === 'confirmed') pending.push(wb.state.bulkReviewed(value))
    if (event === 'close') wb.state.bulkReviewOpen.value = false
  })
  assert.equal(panel.state.error.value, ''); assert.equal(panel.state.fieldCount.value, 3)
  const html = await panel.render()
  for (const field of initial.fields) assert.ok(html.includes(escaped(field.text)))
  await panel.state.confirm(); assert.equal(env.writes().length, 0)
  panel.state.acknowledged.value = true; await panel.state.confirm(); await Promise.all(pending); await flush()
  assert.equal(env.writes().length, 1); assert.equal(wb.state.error.value, '')
  assert.deepEqual(wb.state.preparation.value.fieldSelections.map((s: any) => s.fieldValue), initial.fields.map(f => f.id))
  assert.match(await wb.render(), /此修订已明确选入本次请求/)
})

test('multi-shot page: original page selection and confirmed handler send exactly the listed shots once', async t => {
  const env = await harness(t), inputs = [records(1), records(2), records(3)], rows = [preview(inputs[2]!), preview(inputs[0]!)]
  env.handlers.set('get /execution-preparations/groups/', (_, config) => {
    assert.deepEqual(config.params, { stage_run_id: stageId, page: 1, kind: 'primary' })
    return page(inputs.map(input => ({ unit_key: input.preparation.unit_key, label: input.preparation.presentation.label,
      version_count: 1, latest: input.preparation })))
  })
  env.handlers.set('post /execution-preparations/bulk-field-preview/', payload => {
    assert.deepEqual(payload.preparation_ids, rows.map(row => row.preparation_id)); return { items: rows }
  })
  let release!: (v: unknown) => void
  env.handlers.set('post /execution-preparations/bulk-field-confirm/', payload => {
    assert.deepEqual(payload, { items: rows.map(receipt) })
    return new Promise(resolve => { release = resolve })
  })
  const events: any[] = [], stage = await env.mount('StageExecutionPreparations', { stageId }, (...e) => events.push(e))
  assert.equal(stage.state.error.value, ''); stage.state.togglePage()
  assert.deepEqual(stage.state.checkedIds.value, inputs.map(input => input.preparation.id))
  stage.state.checkedIds.value = rows.map(row => row.preparation_id)
  assert.match(await stage.render(), /已选 2 镜/)
  const panel = await env.mount('ExecutionFieldBulkReview', { preparationIds: [...stage.state.checkedIds.value] }, (event, value) => {
    if (event === 'busy') stage.state.setBusy(value)
    if (event === 'confirmed') stage.state.bulkReviewed(value)
  })
  panel.state.acknowledged.value = true
  const submitting = panel.state.confirm(); await flush(); await panel.state.confirm()
  assert.equal(env.writes().length, 1); assert.equal(stage.state.busy.value, true)
  release({ confirmed_field_count: 6, items: rows.map(row => ({ preparation_id: row.preparation_id, revision: 8 })) })
  await submitting; await flush()
  assert.match(stage.state.bulkNotice.value, /已确认并采用 6 个字段/)
  assert.deepEqual(stage.state.checkedIds.value, []); assert.ok(events.some(([e]) => e === 'changed'))
  assert.ok(!JSON.stringify(env.writes()).includes(inputs[1]!.preparation.id), 'unlisted shot has no command')
})

test('new pending revision never replaces the adopted body; mixed batch keeps it and replacement carries original CAS', async t => {
  const env = await harness(t), initial = records(), adopted = reviewedSnapshot(initial)
  initial.fields[0] = adopted.fields[0]; initial.preparation.revision = 8
  initial.preparation.field_selections = [adopted.preparation.field_selections[0]]
  const old = initial.fields[0]!
  const newer = { ...clone(old), id: id(900), revision: 2, text: 'New unadopted body.', text_sha256: digest('new'),
    status: 'pending', approval_kind: null, approval_label: '尚未审核', reviewed_by: null, reviewed_at: null }
  initial.fields.push(newer); initial.preparation.field_slots[0].latest_value_id = newer.id
  initial.preparation.field_slots[0].next_revision = 3
  env.serve(initial)
  env.handlers.set('get /execution-field-values/', (_, config) => {
    assert.equal(config.params.preparation_id, initial.preparation.id); return page([newer, old, ...initial.fields.slice(1, 3)])
  })
  const wb = await env.workbench(initial)
  assert.equal(wb.state.field.value.id, old.id); await wb.state.loadHistory()
  assert.deepEqual(wb.state.history.value.results.slice(0, 2).map((f: any) => [f.id, f.revision]), [[newer.id, 2], [old.id, 1]])
  const mixed = preview({ ...initial, fields: initial.fields.slice(0, 3) })
  Object.assign(mixed.fields[0]!, { state: 'selected', reason: '已采用，保持不变', revision: null, text: '' })
  env.handlers.set('post /execution-preparations/bulk-field-preview/', () => ({ items: [mixed] }))
  env.handlers.set('post /execution-preparations/bulk-field-confirm/', payload => {
    assert.deepEqual(payload, { items: [receipt(mixed)] })
    // Explicit post-review receipt retains v1 and its original selection revision.
    const approved = clone(initial); approved.preparation.revision = 10
    approved.preparation.field_selections = adopted.preparation.field_selections
    approved.fields = [...adopted.fields, newer]; env.serve(approved)
    return { confirmed_field_count: 2, items: [{ preparation_id: initial.preparation.id, revision: 10 }] }
  })
  const bulk = await env.mount('ExecutionFieldBulkReview', { preparationIds: [initial.preparation.id] })
  assert.equal(bulk.state.fieldCount.value, 2)
  assert.match(await bulk.render(), /已采用，保持不变/); assert.doesNotMatch(await bulk.render(), /New unadopted body/)
  bulk.state.acknowledged.value = true; await bulk.state.confirm()
  await wb.state.load(); await wb.state.openSlot(wb.state.preparation.value.fieldSlots[0])
  assert.equal(wb.state.field.value.id, old.id)
  assert.deepEqual(wb.state.preparation.value.fieldSelections[0], {
    id: initial.preparation.field_selections[0].id, preparation: initial.preparation.id, unitKey: old.unit_key,
    fieldKey: old.field_key, fieldValue: old.id, revision: 1, selectedBy: 1, selectedAt: stamp,
  })
  await wb.state.openField(newer.id)
  assert.match(await wb.render(), /尚未采用这份正文/)
  await wb.state.review('select'); assert.equal(env.writes().length, 1)
  assert.match(wb.state.error.value, /明确确认替换/)
  await wb.state.load(); await wb.state.openSlot(wb.state.preparation.value.fieldSlots[0])
  assert.equal(wb.state.field.value.id, old.id)
  await wb.state.openField(newer.id); wb.state.replaceAcknowledged.value = true
  env.handlers.set(`post /execution-field-values/${newer.id}/confirm/`, payload => {
    assert.deepEqual(payload, { expected_preparation_revision: 10, expected_text_sha256: newer.text_sha256,
      expected_source_sha256: newer.source_sha256, select_for_execution: true, replace: true, expected_selection_revision: 1 })
    return conflict('采用记录已更新；字段来源或规则变化，未覆盖原版本。')
  })
  await wb.state.review('select')
  assert.match(await wb.render(), /采用记录已更新；字段来源或规则变化，未覆盖原版本。/)
  assert.equal(wb.state.preparation.value.fieldSelections[0].fieldValue, old.id)
  assert.equal(wb.state.needsRefresh.value, true)
  await wb.state.review('select'); assert.equal(env.writes().length, 2)
})

test('stale bulk conflict is shown verbatim, emits no success and requires rereading the exact scope', async t => {
  const env = await harness(t), inputs = [records(1), records(2)], rows = inputs.map(preview), events: any[] = []
  const reason = '准备修订已更新；本批未采用任何字段。请重新核对确切版本。'
  env.handlers.set('post /execution-preparations/bulk-field-preview/', () => ({ items: rows }))
  env.handlers.set('post /execution-preparations/bulk-field-confirm/', payload => {
    assert.deepEqual(payload, { items: rows.map(receipt) }); return conflict(reason)
  })
  const panel = await env.mount('ExecutionFieldBulkReview', { preparationIds: inputs.map(input => input.preparation.id) }, (...e) => events.push(e))
  panel.state.acknowledged.value = true; await panel.state.confirm()
  assert.match(await panel.render(), new RegExp(reason)); assert.equal(panel.state.stale.value, true)
  assert.equal(panel.state.acknowledged.value, false); assert.ok(!events.some(([e]) => e === 'confirmed' || e === 'close'))
  panel.state.acknowledged.value = true; await panel.state.confirm(); assert.equal(env.writes().length, 1)
  const blocked = clone(rows[1]!)
  blocked.ready = false; blocked.reason = '多个可选修订；不会自动选择最新版本。'
  Object.assign(blocked.fields[0]!, { state: 'blocked', value_id: null, revision: null, text: '', reason: blocked.reason })
  env.handlers.set('post /execution-preparations/bulk-field-preview/', () => ({ items: [rows[0], blocked] }))
  await panel.state.load(); await flush()
  assert.match(await panel.render(), /多个可选修订；不会自动选择最新版本/)
  assert.deepEqual(panel.state.selected.value, [rows[0]!.preparation_id])
  assert.equal(panel.state.acknowledged.value, false); assert.equal(env.writes().length, 1)
})

test('cross-preparation reuse reads original IDs and approval without creating or reapproving a local copy', async t => {
  const env = await harness(t), source = reviewedSnapshot(records()), reused = clone(source)
  reused.preparation.id = id(800); reused.preparation.request_key = 'candidate-2'
  reused.preparation.field_selections.forEach((s: any) => { s.preparation = reused.preparation.id })
  env.serve(reused)
  env.handlers.set('get /execution-field-values/', (_, config) => {
    assert.equal(config.params.preparation_id, reused.preparation.id); return page([])
  })
  const wb = await env.workbench(reused)
  for (const slot of wb.state.preparation.value.fieldSlots) {
    await wb.state.openSlot(slot)
    assert.equal(wb.state.localField.value, false)
    const field = source.fields.find(f => f.field_key === slot.fieldKey)!
    assert.equal(wb.state.field.value.id, field.id); assert.equal(wb.state.field.value.text, field.text)
    const html = await wb.render()
    assert.match(html, /从其他准备复用的只读修订/); assert.match(html, /此修订已明确选入本次请求/)
    assert.doesNotMatch(html, />确认并采用此修订<|>仅确认，不采用</)
    await wb.state.review('select'); await wb.state.review('confirm')
  }
  await wb.state.loadHistory(); assert.equal(wb.state.history.value.count, 0)
  assert.equal(env.writes().length, 0)
})

test('policy-approved selections are readable and continuable without manufacturing per-field human review', async t => {
  const env = await harness(t), input = reviewedSnapshot(records(), 'policy')
  input.preparation.status = 'preparing'
  input.preparation.review_contract = { schema_version: 1, mode: 'policy', policy }
  input.preparation.review_summary = { mode: 'policy', label: '自动策略批准', description: '按已冻结策略选择齐备字段，不要求逐字段人工确认。' }
  const pending = { ...clone(input.fields[0]), id: id(902), revision: 2, text: 'Later policy candidate, not selected.',
    status: 'pending', approval_kind: null, approval_label: '尚未审核', reviewed_by: null, reviewed_at: null,
    approval_policy_sha256: null, text_sha256: digest('later-policy') }
  input.fields.push(pending); input.preparation.field_slots[0].latest_value_id = pending.id
  input.preparation.field_slots[0].next_revision = 3
  env.serve(input)
  const wb = await env.workbench(input)
  assert.equal(wb.state.error.value, ''); assert.equal(wb.state.canAdvance.value, true)
  assert.equal(wb.state.preparation.value.reviewSummary.mode, 'policy')
  for (const slot of wb.state.preparation.value.fieldSlots) {
    await wb.state.openSlot(slot)
    assert.notEqual(wb.state.field.value.id, pending.id)
    assert.equal(wb.state.field.value.approvalKind, 'policy'); assert.equal(wb.state.field.value.reviewedBy, null)
    assert.equal(wb.state.field.value.approvalPolicySha256, digest('policy'))
    assert.match(await wb.render(), /自动策略批准/)
  }
  assert.equal(env.writes().length, 0, 'no confirm, selection, continue or dispatch on read')
})

test('policy ambiguity preserves an explicit older version choice, not an automatic newest-version fallback', async t => {
  const env = await harness(t), input = records(), old = input.fields[0]!
  input.preparation.review_contract = { schema_version: 1, mode: 'policy', policy }
  input.preparation.review_summary = { mode: 'policy', label: '自动策略', description: '多个可选修订需要明确版本；其余字段沿策略执行。' }
  input.preparation.reason_code = 'field_selection_conflict'
  input.preparation.reason = '多个可选修订，不会自动选择最新版本。'
  const newer = { ...clone(old), id: id(901), revision: 2, text: 'Another pending candidate.', text_sha256: digest('another') }
  input.fields.push(newer); input.preparation.field_slots[0].latest_value_id = newer.id
  input.preparation.field_slots[0].next_revision = 3
  env.serve(input)
  const wb = await env.workbench(input)
  assert.match(await wb.render(), /不会自动选择最新版本/)
  assert.equal(wb.state.preparation.value.fieldSelections.length, 0)
  await wb.state.openField(old.id)
  const after = reviewedSnapshot(records(), 'policy')
  after.preparation.review_contract = input.preparation.review_contract
  after.preparation.review_summary = input.preparation.review_summary
  Object.assign(after.fields[0]!, { approval_kind: 'human', approval_label: '人工审核', reviewed_by: 1, approval_policy_sha256: null })
  after.preparation.field_selections[0].selected_by = 1
  after.fields.push(newer); after.preparation.field_slots[0].latest_value_id = newer.id
  after.preparation.field_slots[0].next_revision = 3
  env.handlers.set(`post /execution-field-values/${old.id}/confirm/`, payload => {
    assert.deepEqual(payload, { expected_preparation_revision: 4, expected_text_sha256: old.text_sha256,
      expected_source_sha256: old.source_sha256, select_for_execution: true, replace: false, expected_selection_revision: null })
    env.serve(after)
    // Only this version is manually confirmed. The subsequent GET is a declared
    // server-policy result; the test implements no evaluator for the other fields.
    return { field: after.fields[0], preparation: { ...input.preparation, revision: 6 }, selection: after.preparation.field_selections[0] }
  })
  await wb.state.review('select'); assert.equal(wb.state.error.value, '')
  assert.equal(env.writes().length, 1)
  assert.equal(wb.state.preparation.value.fieldSelections[0].fieldValue, old.id)
  await wb.state.openSlot(wb.state.preparation.value.fieldSlots[1])
  assert.equal(wb.state.field.value.approvalKind, 'policy'); assert.equal(wb.state.field.value.reviewedBy, null)
})
