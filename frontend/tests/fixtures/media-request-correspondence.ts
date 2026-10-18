// Batch 279 presentation receipts, aligned to the batch 278 contract (not exported live data).
// No backend compiler, model, database, Smoke or delivery runs in this fixture.
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import StageExecutionPreparations from '../../src/components/StageExecutionPreparations.vue'
import ShotExecutionEvidence from '../../src/components/ShotExecutionEvidence.vue'
import { api } from '../../src/services/api'
import type { ProductionRun } from '../../src/services/platformTypes'
import baseStyles from '../../src/styles.css?inline'
import '../../src/platform.css'

type Raw = Record<string, any>
export const choices = ['manual-comfy', 'recipe-comfy', 'manual-rest', 'recipe-rest'] as const
export type Choice = typeof choices[number]
export const id = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
const stamp = '2026-10-04T00:00:00Z', sha = 'a'.repeat(64)
const clone = <T>(value: T): T => structuredClone(value)
const page = (results: unknown[]) => ({ count: results.length, next: null, previous: null, results })

export function records(choice: Choice) {
  const n = (choices.indexOf(choice) + 1) * 100, comfy = choice.endsWith('comfy')
  const projectId = id(n), runId = id(n + 1), stageId = id(n + 2), shotId = id(n + 3)
  const preparationId = id(n + 4), versionId = id(n + 5), taskId = id(n + 6), attemptId = id(n + 7)
  const profileId = id(n + 8), definitionId = id(n + 9), unit = `shot:${shotId}`, fieldKey = 'm7_direction'
  const selectedText = `${choice} adopted A: The courier opens the parcel.\n原对白：别动。<script>仅为原文</script>😀`
  const pendingText = `${choice} pending B: THIS MUST NOT REPLACE A`
  // Explicit fixture correspondence: created [red,blue], selected/transmitted [blue,red].
  // Both frozen selections use v1; today's red v2 never enters these receipts.
  const media = comfy ? [1, 0].map((sourceIndex, slot) => ({
    item_key: `use-${n}-${sourceIndex}`, input_key: 'references', asset_id: id(n + 20 + sourceIndex), artifact_id: null,
    display_name: 'Same parcel', kind: 'image', preview_url: '', purpose: null, paired_to: null,
    source_sha256: String(sourceIndex + 1).repeat(64), checksum: String(sourceIndex + 3).repeat(64),
    input_label: `<Picture ${slot + 1}>`, ports: [{ target_key: 'reference_images', port: { kind: 'semantic', semantic_key: 'image.reference' } }],
    staging_status: 'staged', reference_binding: {
      position: slot, original_name: 'Same parcel', reference_name: `Same parcel [use-${n}-${sourceIndex}]`, name_disambiguated: true,
      input_slot: slot + 1, shot_reference: { use_id: id(n + 30 + sourceIndex), use_key: `parcel-${sourceIndex}`,
        position: slot, purpose: '原选择：包裹外观', source_mode: 'existing',
        source: { kind: 'reference_version', reference_version_id: id(n + 40 + sourceIndex), series_id: id(n + 50 + sourceIndex),
          version: 1, acquisition_source: 'existing' }, setting: null },
    },
  })) : []
  const parameters: Raw = comfy ? { 'image.width': 320, 'image.height': 320, 'sampling.seed': 0 }
    : { 'image.width': 768, 'image.height': 768, 'video.fps': 24, 'video.duration_seconds': 5 }
  const positive = [selectedText, ...media.map(row => `${row.input_label} ${row.reference_binding.reference_name}`)].join('\n')
  const requestSha = String(choices.indexOf(choice) + 1).repeat(64)
  const fieldA: Raw = { id: id(n + 10), preparation: preparationId, unit_key: unit, field_key: fieldKey, revision: 1,
    source_sha256: sha, field_contract_sha256: sha, text_sha256: sha, origin_kind: 'model', origin_attempt: id(n + 70),
    origin_asset_job: null, status: 'confirmed', approval_kind: 'human', approval_label: '人工审核', reviewed_by: 1,
    reviewed_at: stamp, approval_policy_sha256: null, created_by: 1, created_at: stamp, updated_at: stamp, warnings: [], text: selectedText }
  const fieldB = { ...fieldA, id: id(n + 11), revision: 2, status: 'pending', approval_kind: null, approval_label: '尚未审核',
    reviewed_by: null, reviewed_at: null, text: pendingText }
  const fieldContract = { field_key: fieldKey, field_definition: { field_definition_id: id(n + 12), field_definition_version_id: definitionId,
    key: fieldKey, label: '冻结镜头动作', version: 1 }, max_chars: 1000, generation_required: true, required: true,
    generation_instruction: 'Only the original selected facts.', generation_mode: 'model_generated', language_policy: 'any', source_selector: 'request.structured_input' }
  const preparation: Raw = {
    id: preparationId, stage_run: stageId, asset_job: null, deployment_smoke: null, workflow_smoke: null,
    execution_instance_version: versionId, purpose: 'production', unit_key: unit, request_key: 'candidate-1', revision: 8,
    status: 'ready', intent_sha256: sha, request_sha256: requestSha, request_frozen_at: stamp,
    next_check_at: null, reason_code: '', reason: '', evaluation_revision: null, evaluated_at: null, evaluation: null,
    cancellation: { allowed: false, reason: '只读隔离样例，不提供取消或提交。', cancelled_at: null, cancelled_by: null },
    presentation: { kind: 'primary', label: `${choice} · 原镜头准备`, operation: 'image.generate', parent: null, task: null },
    outcome: { message: '隔离冻结请求回执；不代表真实发送、产物或采用。', artifact_count: 0, selected_count: 0 },
    created_by: 1, created_at: stamp, updated_at: stamp, intent: {},
    resolved_inputs: { schema_version: 1, inputs: { logical_values: { source_text: `${choice} original source` } },
      parameters: { values: parameters, sources: Object.fromEntries(Object.keys(parameters).map(key => [key, `instance.${versionId}.parameters`])),
        contracts: Object.fromEntries(Object.entries(parameters).map(([key, value]) => [key, { rule: { type: 'integer', required: true, const: value },
          value_source: `instance.${versionId}.parameters`, layers: [{ source: `instance.${versionId}.parameters`, rule: { type: 'integer', required: true, const: value } }] }])) },
      media_generation_target: { schema_version: 1, source_mode: 'generate', media_type: 'image',
        execution_instance_version_id: versionId, definition_sha256: sha, inputs_sha256: sha,
        candidates: { count: 1, evaluator: null }, setting: null },
      profile_sources: { profile: { version_id: profileId, contract_sha256: sha } },
      field_requests: [{ schema_version: 1, size_bytes: 1, sha256: sha, value: { schema_version: 1, operation: 'prompt.fields.generate',
        profile: { version_id: profileId, contract_sha256: sha }, items: [{ unit_key: unit, field_contract: fieldContract }] } }],
    },
    review_contract: { schema_version: 1, mode: 'manual' },
    review_summary: { mode: 'manual', label: '已保存采用依据', description: 'A 已确认并采用；B 是另一个待审修订。夹具不执行审核。' },
    prepared_request: { schema_version: 1, sha256: requestSha, size_bytes: 1 }, capacity_sample: null,
    response_diagnostic: null, resource_review: null, text_response_review: null, auxiliary_batches: [],
    field_selections: [{ id: id(n + 13), preparation: preparationId, unit_key: unit, field_key: fieldKey, field_value: fieldA.id,
      revision: 1, selected_by: 1, selected_at: stamp }],
    field_slots: [{ unit_key: unit, field_key: fieldKey, field_label: '冻结镜头动作', next_revision: 3, latest_value_id: fieldB.id,
      source_language: 'Chinese', target_language: 'English', effective_language: 'any', field_contract: fieldContract }],
  }
  const channels = [{ key: 'positive_prompt', text: positive, character_count: Array.from(positive).length }]
  const prompt: Raw = { preparation_id: preparationId, preparation_revision: 8, mode: 'frozen', request_sha256: requestSha,
    frozen_at: stamp, prompt_mode: 'profile', content_kind: 'channels', messages: [],
    profile_basis: { version_id: profileId, version: 1, contract_sha256: sha }, channels,
    length_diagnostics: [{ channel: 'positive_prompt', character_count: channels[0]!.character_count, max_chars: 2000, over_by: 0,
      limit_sources: [{ source: 'profile.positive_prompt', max_chars: 2000 }], fragments: [{ field_key: fieldKey, label: '冻结完整组装',
        field_definition_version_id: definitionId, source_selector: 'request.structured_input', max_chars: 2000,
        text_chars: channels[0]!.character_count, separator_chars: 0 }] }],
    field_basis: [{ unit_key: unit, field_key: fieldKey, field_value_id: fieldA.id, revision: 1, origin: 'adopted', status: 'confirmed' }],
    sources: { references: media.map(row => ({ label: row.input_label, name: row.reference_binding.reference_name, category: 'prop', guidance: '' })),
      dialogue: [{ unit_key: unit, subject: '信使', text: '别动。', start_ms: 1000, end_ms: 2000, voiceover: false, delivery: '轻声' }] }, issues: [] }
  const task: Raw = { id: taskId, project: projectId, kind: 'image', status: 'queued', created_at: stamp, output_media: [],
    execution: { production_run_id: runId, stage_run_id: stageId, stage_key: 'shot_media_generation', stage_ordinal: 1, unit_key: unit,
      attempt_id: attemptId, attempt_number: 1, preparation_id: preparationId, execution_instance_version_id: versionId, operation: 'image.generate' },
    input_contract_status: 'available', input_contract_code: '', input_parameters: {
      preparation_id: preparationId, attempt_id: attemptId, execution_instance_version_id: versionId, workflow_version_id: id(n + 60),
      request_sha256: requestSha, operation: 'image.generate', output_kind: 'image', prompt_mode: 'profile', prompt_profile_version_id: profileId,
      parameters: Object.entries(parameters).map(([key, value]) => ({ key, value, value_source: `instance.${versionId}.parameters`, rule: { type: 'integer', required: true, const: value } })),
      prompt_channels: channels.map(row => ({ key: row.key, semantic_key: 'prompt.positive', character_count: row.character_count })), media_uses: media,
    } }
  const taskPrompt: Raw = { task_id: taskId, attempt_id: attemptId, preparation_id: preparationId, execution_instance_version_id: versionId,
    request_sha256: requestSha, prompt_mode: 'profile', prompt_profile_version_id: profileId,
    declared_reference_labels: media.map(row => row.input_label), channels: channels.map(row => ({ ...row, semantic_key: 'prompt.positive', value_source: 'profile' })) }
  // Exact schema3 route context; not the obsolete mode=auto/routing={} provenance fixture.
  const run = { id: runId, project: projectId, planSource: choice.startsWith('manual') ? 'manual' : 'recipe',
    executionPlan: { schemaVersion: 1, sizeBytes: 1, sha256: sha, value: { recipe: { definition: { schema_version: 3, stages: [{
      key: 'shot_media_generation', name: '隔离媒体阶段', kind: 'image', mode: 'existing_pipeline', approval: 'auto',
      routing: { mode: 'fixed', routes: [{ execution_instance_version_id: versionId, expected_definition_sha256: sha, input_sources: {}, parameter_policies: [] }] },
    }] } } } } } as unknown as ProductionRun
  return { choice, comfy, projectId, runId, stageId, shotId, preparationId, versionId, taskId, attemptId, profileId, definitionId,
    selectedText, pendingText, requestSha, parameters, media, fieldA, fieldB, preparation, prompt, task, taskPrompt, run,
    currentCatalog: { versionId: id(n + 90), width: comfy ? 640 : 1024, referenceName: 'TODAY RENAMED', referenceVersionId: id(n + 91) } }
}

export function createFixture(transport: any = globalThis) {
  const mode = ref<Choice>('manual-comfy'), scenario = ref('normal'), events = ref<Raw[]>([]), blocked = ref<string[]>([])
  const cases = Object.fromEntries(choices.map(choice => [choice, records(choice)])) as Record<Choice, ReturnType<typeof records>>
  let transform: (config: any, body: any) => Promise<any> = async (_config, body) => body
  const deny = (label: string): never => { blocked.value.push(label); throw Error('M7 fixture blocked: ' + label) }
  const replacements: Array<() => void> = []
  function replace(target: any, key: string, value: any) {
    if (!target) return
    const descriptor = Object.getOwnPropertyDescriptor(target, key)
    Object.defineProperty(target, key, { configurable: true, writable: true, value })
    replacements.push(() => { if (descriptor) Object.defineProperty(target, key, descriptor); else delete target[key] })
  }
  replace(transport, 'fetch', () => deny('fetch'))
  replace(transport.XMLHttpRequest?.prototype, 'send', () => deny('XHR'))
  replace(transport.navigator, 'sendBeacon', () => deny('beacon'))
  replace(transport, 'WebSocket', function () { return deny('WebSocket') })
  replace(transport, 'EventSource', function () { return deny('EventSource') })
  // Replace request interceptors before any request: never read real localStorage/credentials.
  const manager = api.interceptors.request as any, previousHandlers = manager.handlers.slice(), oldAdapter = api.defaults.adapter
  manager.clear()
  const adapter = async (config: any) => {
    const url = String(config.url), params = config.params || {}, method = String(config.method).toUpperCase()
    if (method !== 'GET' || config.data !== undefined || config.headers?.get('Authorization')) return deny(`${method} ${url}`)
    let body: any, found = false
    for (const row of Object.values(cases)) {
      if (url === '/execution-preparations/groups/' && params.stage_run_id === row.stageId && params.kind === 'primary' && params.page === 1) {
        body = page([{ unit_key: row.preparation.unit_key, label: row.preparation.presentation.label, version_count: 1, latest: row.preparation }]); found = true
      } else if (url === `/execution-preparations/${row.preparationId}/`) { body = row.preparation; found = true }
      else if (url === `/execution-preparations/${row.preparationId}/prompt-display/` && params.expected_preparation_revision === row.preparation.revision) { body = row.prompt; found = true }
      else if (url === '/execution-field-values/' && params.preparation_id === row.preparationId && params.page === 1) { body = page([row.fieldB, row.fieldA]); found = true }
      else if (url === `/execution-field-values/${row.fieldA.id}/`) { body = row.fieldA; found = true }
      else if (url === `/execution-field-values/${row.fieldB.id}/`) { body = row.fieldB; found = true }
      else if (row.comfy && url === '/tasks/' && params.project === row.projectId && params.unit_key === `shot:${row.shotId}` && params.page_size === 50) { body = page([row.task]); found = true }
      else if (row.comfy && url === `/tasks/${row.taskId}/`) { body = row.task; found = true }
      else if (row.comfy && url === `/tasks/${row.taskId}/input-prompt/`) { body = row.taskPrompt; found = true }
      if (found) break
    }
    if (!found) return deny(`${method} ${url} ${JSON.stringify(params)}`)
    events.value.push({ method, url, params: clone(params) })
    if (scenario.value === 'read-failure' && /\/execution-preparations\/[^/]+\/$/.test(url)) throw Error('隔离：准备读取失败')
    if (scenario.value === 'prompt-failure' && url.endsWith('/prompt-display/')) throw Error('隔离：提示词读取失败')
    if (scenario.value === 'task-failure' && /^\/tasks\/[^/]+\/$/.test(url)) throw Error('隔离：任务读取失败')
    body = clone(body)
    if (scenario.value === 'identity-mismatch' && url.endsWith('/input-prompt/')) body.preparation_id = id(99999)
    return { data: await transform(config, body), status: 200, statusText: 'ISOLATED FIXTURE', headers: {}, config }
  }
  api.defaults.adapter = adapter
  manager.use((config: any) => {
    if (config.adapter !== adapter) return deny('foreign adapter')
    config.headers.delete('Authorization'); return config
  })
  const pinia = createPinia()
  const Page = { setup: () => () => {
    const row = cases[mode.value]
    return h('div', { class: 'm7-panels' }, [
      h(StageExecutionPreparations, { key: row.runId, runId: row.runId, stageId: row.stageId, ...(row.run.planSource === 'manual' ? { manualRun: row.run } : {}) }),
      row.comfy ? h(ShotExecutionEvidence, { projectId: row.projectId, shotId: row.shotId })
        : h('p', { role: 'status' }, 'REST 仅标量请求：此切片不伪造 Comfy 工作流任务或参考槽位。'),
    ])
  } }
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'm7', component: Page },
    { path: '/workflows', name: 'workflows', component: { render: () => h('p', '隔离任务定位；不访问正式任务中心。') } },
    { path: '/:pathMatch(.*)*', component: { render: () => h('p', '隔离内存导航；不访问正式页面。') } }] })
  async function choose(value: Choice) {
    if (!choices.includes(value)) return deny('unknown fixture case')
    mode.value = value; scenario.value = 'normal'; await router.replace({ name: 'm7', query: { case: value } })
  }
  const Root = { setup: () => () => h('main', { class: 'm7-fixture', onClickCapture: (event: any) => {
    const anchor = event.target?.closest?.('a')
    if (anchor && !anchor.getAttribute('href')?.startsWith('#')) { event.preventDefault(); event.stopPropagation() }
  } }, [h('style', baseStyles + '\n.m7-fixture{max-width:1180px;margin:auto;padding:16px;overflow-wrap:anywhere}.m7-fixture nav{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.m7-panels{display:grid;gap:16px;min-width:0}.m7-fixture button{white-space:normal}'),
    h('h1', 'M7 请求对应 · 隔离页面夹具'), h('p', '仅预置回执；零现用业务、零模型、零外部传输。不是第278批后台测试或真实发送的再次验收。'),
    h('nav', { 'aria-label': '隔离组合' }, choices.map(value => h('button', { type: 'button', onClick: () => choose(value), 'aria-pressed': mode.value === value }, value))),
    h('nav', { 'aria-label': '隔离响应情景' }, ['normal', 'read-failure', 'prompt-failure', 'task-failure', 'identity-mismatch'].map(value =>
      h('button', { type: 'button', onClick: () => { scenario.value = value }, 'aria-pressed': scenario.value === value }, value))),
    h('p', { role: 'status' }, `情景 ${scenario.value} · GET ${events.value.length} · 阻断 ${blocked.value.length} · 业务写入 0`), h(RouterView),
  ]) }
  const restore = () => { api.defaults.adapter = oldAdapter; manager.handlers = previousHandlers; replacements.reverse().forEach(fn => fn()) }
  return { Root, router, pinia, mode, scenario, cases, events, blocked, choose, restore,
    setResponse: (fn: typeof transform) => { transform = fn } }
}

if (typeof document !== 'undefined' && document.querySelector('[data-m7-correspondence]')) {
  const fixture = createFixture()
  void fixture.choose('manual-comfy').then(() => fixture.router.isReady()).then(() => {
    createApp(fixture.Root).use(fixture.pinia).use(fixture.router).mount('[data-m7-correspondence]')
  })
}
