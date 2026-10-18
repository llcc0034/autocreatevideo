// Synthetic HTTP receipts only: original forms, adapters and relation reader remain authoritative.
import { createApp, h, ref } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import type { AxiosAdapter } from 'axios'
import type { AssetGenerationJob } from '../../src/types'
import AssetExecutionCreateForm from '../../src/components/AssetExecutionCreateForm.vue'
import AssetReferenceBrowser from '../../src/components/AssetReferenceBrowser.vue'
import { api } from '../../src/services/api'
import { repository } from '../../src/services/repository'
import { unusedLanguageForm } from './executionLanguage'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

type Raw = Record<string, any>
type Role = 'primary' | 'variant'
export const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
export const ids = { primaryInstance: id(1), variantInstance: id(2), primaryJob: id(11), variantJob: id(12),
  primarySeries: id(21), variantSeries: id(22), primaryVersion: id(31), variantVersion: id(32),
  primaryAsset: id(41), variantAsset: id(42), wrongAsset: id(43) }
export const primaryText = 'MOCK 主图：送信人站立，灰色外套。'
export const differenceText = '  MOCK 差异：仅将外套改为蓝色，保留同一人物与面容。\n不重新设计主图。  '
const sha = 'a'.repeat(64), stamp = '2026-10-04T00:00:00Z'
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
const page = (results: unknown[]) => ({ count: results.length, next: null, previous: null, results })
const ordered = (value: any): any => Array.isArray(value) ? value.map(ordered) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])])) : value
const same = (a: unknown, b: unknown) => JSON.stringify(ordered(a)) === JSON.stringify(ordered(b))
const exactKeys = (value: Raw, keys: string[]) => value && same(Object.keys(value).sort(), [...keys].sort())
export const metadata = (role: Role) => ({ name: role === 'primary' ? 'MOCK 主图 P' : 'MOCK 变体 V', description: '', styleId: '', imageCategory: 'reference' as const })
export const option = (role: Role) => ({ execution_instance_version_id: role === 'primary' ? ids.primaryInstance : ids.variantInstance,
  key: 'mock-' + role, name: role === 'primary' ? 'MOCK 主图文生图实例' : 'MOCK 参考主图变体实例', version: 1,
  operation: role === 'primary' ? 'image.text_to_image' : 'image.image_to_image', mode: '', prompt_mode: 'manual', definition_sha256: sha })
export function contract(role: Role): Raw {
  const selected = option(role)
  return { version_id: selected.execution_instance_version_id, revision: 1, frozen: true, schema_version: 1,
    size_bytes: 1024, sha256: sha, target_fingerprint: 'b'.repeat(64), context_fields: [], language_form: unusedLanguageForm(),
    input_forms: {}, subject_categories: [{ value: 'character', label: '人物' }],
    appearance_inputs: { primary_inputs: role === 'primary' ? [] : [{ input_key: 'reference_1', target_key: 'primary_image' }],
      difference_sources: [{ kind: 'manual_channel', key: 'positive_prompt' }] },
    definition: { version_id: selected.execution_instance_version_id,
      configuration: { deployment_id: id(3), operation: selected.operation, mode: '', workflow_version_id: id(4),
        prompt_mode: 'manual', prompt_profile_version_id: null, parameters: [], auxiliaries: [],
        inputs: role === 'primary' ? [] : [{ key: 'reference_1', kind: 'image', position: 0, required: true, min_items: 1, max_items: 1,
          semantic_contract: { schema_version: 1, allowed_purposes: [], allowed_subject_kinds: ['external'], allowed_scope_kinds: ['request'], paired_with_input_key: null, allow_repeated_asset: true },
          targets: [{ target_kind: 'workflow_slot', target_key: 'primary_image', is_primary: true }] }] },
      interface: { output: { contract: { primary: { kind: 'image' } } }, input_purposes: {}, parameters: {},
        prompt: { mode: 'manual', channels: { positive_prompt: { rule: { type: 'string', required: true } } } } } } }
}
export function asset(which: 'primary' | 'variant' | 'wrong'): Raw {
  const name = which === 'primary' ? 'MOCK 主图 P' : which === 'variant' ? 'MOCK 变体 V' : 'MOCK 错误图片（不是 P）'
  return { id: which === 'primary' ? ids.primaryAsset : which === 'variant' ? ids.variantAsset : ids.wrongAsset,
    name, kind: 'image', mime_type: 'image/svg+xml', is_active: true, checksum: 'c'.repeat(64), owner: 1, style: null,
    description: '', prompt_hint: '', metadata: {}, source_info: 'MOCK 合成媒体；不是实际生成',
    preview_url: 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="420" height="240"><rect width="420" height="240" fill="${which === 'variant' ? '#315e86' : which === 'wrong' ? '#843e3e' : '#555d67'}"/><text x="24" y="120" fill="white" font-size="24">MOCK ${which.toUpperCase()} ONLY</text></svg>`) }
}
export function series(role: Role, variantReady = false): Raw {
  return { id: role === 'primary' ? ids.primarySeries : ids.variantSeries, name: metadata(role).name,
    role, owner_id: 1, primary_series_id: role === 'primary' ? null : ids.primarySeries,
    bible_variant_id: null, version_count: 1, variant_count: role === 'primary' && variantReady ? 1 : 0 }
}
export function version(role: Role): Raw {
  const media = asset(role)
  return { id: role === 'primary' ? ids.primaryVersion : ids.variantVersion, series_id: series(role).id, version: 1,
    source: 'generated', asset: { id: media.id, name: media.name, kind: media.kind, mime_type: media.mime_type, is_active: true },
    generated_from_id: role === 'primary' ? null : ids.primaryVersion, setting_revision_id: null,
    generated_artifact: null, generation_job: { id: role === 'primary' ? ids.primaryJob : ids.variantJob, status: 'succeeded' }, created_at: stamp }
}
export function job(role: Role, complete = false): Raw {
  const selected = option(role), jobId = role === 'primary' ? ids.primaryJob : ids.variantJob
  return { id: jobId, retry_of: null, retries: [], execution_instance_version: selected.execution_instance_version_id,
    execution_instance_name: selected.name, execution_version: 1, job_type: 'asset', media_type: 'image', image_category: 'reference',
    name: metadata(role).name, description: '', style: null, execution_kind: 'comfy_workflow', model_deployment: id(3),
    model_deployment_name: 'MOCK 无运行资源', workflow_template_version: id(4), workflow_name: 'MOCK 图片协议', workflow_version: 1,
    output_asset: complete ? asset(role) : null, status: complete ? 'succeeded' : 'preparing', operation: selected.operation,
    is_external: false, allow_external_calls: false, budget_limit: null, estimated_cost: null, actual_cost: null, cost_currency: '',
    result_text: '', remote_status: '', provider_request_id_sha256: '', poll_count: 0, error_code: '', error_message: '', ignore_late_result: false,
    submission_started_at: null, cancel_requested_at: null, cancel_submitted_at: null, created_at: stamp, updated_at: stamp,
    submitted_at: complete ? stamp : null, finished_at: complete ? stamp : null,
    preparation: { id: role === 'primary' ? id(51) : id(52), stage_run: null, asset_job: jobId, deployment_smoke: null, workflow_smoke: null,
      execution_instance_version: selected.execution_instance_version_id, purpose: 'production', unit_key: 'asset', request_key: 'mock-' + role,
      revision: complete ? 2 : 1, status: complete ? 'ready' : 'waiting_fields', intent_sha256: 'd'.repeat(64),
      request_sha256: complete ? 'e'.repeat(64) : null, request_frozen_at: complete ? stamp : null, next_check_at: null,
      reason_code: complete ? '' : 'fields_required', reason: complete ? '' : 'MOCK 创建回执不代表生成成功', evaluation_revision: 1,
      evaluated_at: stamp, created_by: 1, created_at: stamp, updated_at: stamp } }
}

type Transport = { fetch: (...args: any[]) => any; XMLHttpRequest: { prototype: { send: (...args: any[]) => any } }; navigator: { sendBeacon: (...args: any[]) => any }; dispatchEvent?: (event: Event) => unknown }
export function createFixture(transport: Transport) {
  const phase = ref<Role>('primary'), created = ref<AssetGenerationJob | null>(null), completion = ref<AssetGenerationJob | null>(null)
  const primaryReady = ref(false), variantReady = ref(false), refresh = ref(0), busy = ref(false), error = ref('')
  const reads = ref(0), blocked = ref(0), events = ref<Raw[]>([]), posts = ref<Raw[]>([])
  const createdRoles = new Set<Role>(), usedKeys = new Set<string>()
  function record(event: Raw) { events.value = [...events.value.slice(-79), event]; transport.dispatchEvent?.(new CustomEvent('asset-relation-journey:http', { detail: clone(event) })) }
  function deny(method: string, url: string): never {
    blocked.value++; record({ method, url, blocked: true }); throw Error(`MOCK 隔离夹具阻断未声明请求：${method} ${url}`)
  }
  const adapter: AxiosAdapter = async config => {
    const method = String(config.method).toUpperCase(), url = String(config.url), params = config.params || {}
    const payload = typeof config.data === 'string' ? JSON.parse(config.data) : config.data
    let data: any
    const activeRoles = (['primary', 'variant'] as const).filter(role => role === 'primary' ? primaryReady.value : variantReady.value)
    if (method === 'GET' && payload === undefined) {
      if (url === '/asset-generation-jobs/options/' && same(params, { job_type: 'asset', media_type: 'image', page: 1 })) data = page([option(phase.value)])
      else if (url === '/asset-generation-jobs/appearance-options/' && same(params, { page: 1, search: '', role: phase.value })) data = page([])
      else if (url === `/model-execution-instance-versions/${option(phase.value).execution_instance_version_id}/input-contract/` && same(params, {})) data = contract(phase.value)
      else if (url === '/assets/' && exactKeys(params, ['page', 'search', 'is_active']) && params.page === 1 && typeof params.search === 'string' && params.is_active === true) {
        data = page([asset('wrong'), ...activeRoles.map(asset)].filter(row => row.name.includes(params.search)))
      } else if (url === '/asset-reference-series/' && exactKeys(params, ['page', 'search', ...Object.keys(params).filter(key => ['role', 'primary_series_id', 'media_type', 'bible_linked'].includes(key))])
        && params.page === 1 && typeof params.search === 'string' && (!params.role || ['primary', 'variant'].includes(params.role))
        && (!params.primary_series_id || params.primary_series_id === ids.primarySeries) && (!params.media_type || params.media_type === 'image')
        && (params.bible_linked === undefined || typeof params.bible_linked === 'boolean')) {
        data = page(activeRoles.map(role => series(role, variantReady.value)).filter(row => row.name.includes(params.search)
          && (!params.role || row.role === params.role) && (!params.primary_series_id || row.primary_series_id === params.primary_series_id) && params.bible_linked !== true))
      } else {
        for (const role of ['primary', 'variant'] as const) {
          const jobId = role === 'primary' ? ids.primaryJob : ids.variantJob
          if (url === `/asset-generation-jobs/${jobId}/` && same(params, {}) && createdRoles.has(role)) {
            data = job(role, true); if (role === 'primary') primaryReady.value = true; else variantReady.value = true
          }
          if (!activeRoles.includes(role)) continue
          if (url === `/asset-reference-series/${series(role).id}/` && same(params, {})) data = series(role, variantReady.value)
          if (url === `/asset-reference-versions/${version(role).id}/` && same(params, {})) data = version(role)
          if (url === '/asset-reference-versions/' && same(params, { series_id: series(role).id, page: 1 })) data = page([version(role)])
          if (url === `/assets/${asset(role).id}/` && same(params, {})) data = asset(role)
        }
        if (url === `/assets/${ids.wrongAsset}/` && same(params, {})) data = asset('wrong')
      }
    } else if (method === 'OPTIONS' && url === '/asset-reference-series/' && payload === undefined && same(params, {})) {
      data = { actions: { POST: { role: { choices: [{ value: 'primary', display_name: '主图' }, { value: 'variant', display_name: '变体' }] } } } }
    } else if (method === 'POST' && url === '/asset-generation-jobs/' && same(params, {})) {
      const role = phase.value, selected = option(role), target = payload?.appearance_target, key = config.headers.get('Idempotency-Key')
      const base = { execution_instance_version_id: selected.execution_instance_version_id, expected_definition_sha256: sha,
        job_type: 'asset', media_type: 'image', parameters: {}, subject_facts: [], source_language: '', target_language: '', field_values: [],
        style_id: null, image_category: 'reference', name: metadata(role).name, description: '', allow_external_calls: false }
      const input = payload?.inputs?.reference_1?.items?.[0], body = payload?.manual_channels?.positive_prompt
      const expectedTarget = role === 'primary' ? { role: 'primary', setting_revision_id: null, primary_version_id: null, primary_input: null }
        : { role: 'variant', setting_revision_id: null, primary_version_id: ids.primaryVersion,
          primary_input: { input_key: 'reference_1', item_key: input?.item_key }, difference_input: { kind: 'manual_channel', key: 'positive_prompt' } }
      const expectedInputs = role === 'primary' ? {} : { reference_1: { items: [{ kind: 'asset', asset_id: ids.primaryAsset,
        item_key: input?.item_key, purpose: null, scope_ref: { kind: 'request' } }] } }
      if (typeof key !== 'string' || !key || usedKeys.has(key) || createdRoles.has(role) || role === 'variant' && !primaryReady.value
        || typeof body !== 'string' || !body.trim() || role === 'variant' && (typeof input?.item_key !== 'string' || !input.item_key)
        || !same(target, expectedTarget) || !same(payload, { ...base, inputs: expectedInputs, manual_channels: { positive_prompt: body }, appearance_target: expectedTarget })) return deny(method, url)
      posts.value.push({ role, idempotencyKey: key, payload: clone(payload) }); usedKeys.add(key); createdRoles.add(role)
      data = job(role)
    }
    if (data === undefined) return deny(method, url)
    if (data?.preparation) {
      const original = posts.value.find(row => data.id === (row.role === 'primary' ? ids.primaryJob : ids.variantJob))
      if (original) data.preparation.request_key = original.idempotencyKey
    }
    if (method !== 'POST') reads.value++
    record({ method, url, params, ...(payload === undefined ? {} : { payload }), response: data })
    return { data: clone(data), status: method === 'POST' ? 202 : 200, statusText: 'MOCK', config, headers: {} }
  }
  // The original API methods/adapters run; only the final transport is replaced.
  api.interceptors.request.clear(); api.defaults.adapter = adapter
  api.interceptors.request.use(config => { config.adapter = adapter; config.headers.delete('Authorization'); return config })
  const original = { fetch: transport.fetch, send: transport.XMLHttpRequest.prototype.send, beacon: transport.navigator.sendBeacon }
  transport.fetch = async () => deny('fetch', 'raw transport')
  transport.XMLHttpRequest.prototype.send = () => deny('XHR', 'raw transport')
  transport.navigator.sendBeacon = () => deny('beacon', 'raw transport')
  const restore = () => { transport.fetch = original.fetch; transport.XMLHttpRequest.prototype.send = original.send; transport.navigator.sendBeacon = original.beacon }
  function onCreated(value: AssetGenerationJob) { created.value = value; completion.value = null }
  async function readCompletion() {
    if (!created.value || busy.value) return
    busy.value = true; error.value = ''
    try { completion.value = await repository.getAssetGenerationJob(created.value.id); refresh.value++ }
    catch (cause) { error.value = String(cause) }
    finally { busy.value = false }
  }
  function beginVariant() {
    if (busy.value || !primaryReady.value || createdRoles.has('variant')) return
    phase.value = 'variant'; created.value = null; completion.value = null; refresh.value++
  }
  const Journey = { setup: () => () => h('div', [
    h('p', '顺序：主图原表单（角色选“主图”，设定留空）→创建MOCK任务→载入模拟完成回执→填写变体原表单→结果关系回看。'),
    h('p', '变体：先在原图片输入添加一项并选 P，再在外观目标选择确切主图版本P、同一输入项、positive_prompt差异来源；勾选核对后创建。选择“错误图片”时不可创建，不会自动替换。'),
    h('details', [h('summary', '可手工填入的 MOCK 示例正文'), h('pre', primaryText + '\n\n' + differenceText)]),
    !created.value ? h(AssetExecutionCreateForm, { key: phase.value, mediaType: 'image', metadata: metadata(phase.value), onCreated, onBusy: (value: boolean) => { busy.value = value } })
      : h('section', { 'aria-label': 'MOCK 创建回执' }, [h('p', `MOCK 创建已确认：${created.value.id} · ${created.value.status}；不是实际生成。`),
        h('button', { class: 'button primary', disabled: busy.value, onClick: readCompletion }, '载入模拟完成回执（只读GET，非实际执行）')]),
    completion.value ? h('p', { role: 'status' }, `MOCK 完成回执：${completion.value.status} · 原Asset ${completion.value.outputAsset?.id}；请在下方原关系浏览器核对确切版本。`) : null,
    error.value ? h('p', { role: 'alert' }, error.value) : null,
    primaryReady.value && phase.value === 'primary' ? h('button', { class: 'button ghost', disabled: busy.value, onClick: beginVariant }, '填写变体原表单') : null,
    primaryReady.value ? h(AssetReferenceBrowser, { key: refresh.value }) : null,
  ]) }
  const Task = { setup: () => () => h('section', [h('h2', '内存路由查询 · 非实际任务/素材页'), h('pre', JSON.stringify(router.currentRoute.value.query, null, 2)),
    h('button', { class: 'button ghost', onClick: () => router.push({ name: 'journey' }) }, '返回组合夹具')]) }
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'journey', component: Journey },
    { path: '/workflows', name: 'workflows', component: Task }, { path: '/assets', name: 'assets', component: Task }] })
  async function interceptNavigation(event: any) {
    const anchor = event.target?.closest?.('a'); if (!anchor) return
    event.preventDefault(); event.stopPropagation()
    const href = anchor.getAttribute('href') || ''
    if (!href.startsWith('/workflows?') && !href.startsWith('/assets?')) { error.value = '已阻断非夹具导航'; return }
    await router.push(href)
  }
  const Root = { setup: () => () => h('main', { class: 'fixture-shell', onClickCapture: interceptNavigation, onAuxclickCapture: interceptNavigation,
    onContextmenuCapture: (event: any) => { if (event.target?.closest?.('a')) event.preventDefault() } }, [
    h('h1', 'M9-08 主图P → 变体V → 原关系回看'),
    h('aside', { class: 'fixture-warning' }, [h('strong', 'MOCK 隔离模拟 · 零模型 · 零现用业务'),
      h('p', '真实表单/外观编辑器/关系浏览器，只有声明请求返回合成回执。除两次明确的模拟创建外，业务写入及未声明读取全部阻断。此页不证明后端登记、项目/配方数量或真实图片效果。')]),
    h(RouterView), h('p', { role: 'status' }, `MOCK读取：${reads.value} · MOCK创建POST：${posts.value.length} · 未声明/网络阻断：${blocked.value} · 真实业务写入：0`),
    h('details', { class: 'fixture-log' }, [h('summary', '确切请求与声明回执日志'), h('pre', JSON.stringify(events.value, null, 2))]),
  ]) }
  return { Root, router, phase, created, completion, primaryReady, variantReady, reads, posts, blocked, events,
    onCreated, readCompletion, beginVariant, interceptNavigation, restore }
}
export const fixtureBaseStyles = baseStyles.replace(/@import\s+url\([^;]+;/g, '')
if (typeof document !== 'undefined') {
  const fixture = createFixture(window)
  const style = document.createElement('style'); style.textContent = fixtureBaseStyles; document.head.append(style)
  void fixture.router.push('/').then(() => { createApp(fixture.Root).use(fixture.router).mount('#app') })
}
