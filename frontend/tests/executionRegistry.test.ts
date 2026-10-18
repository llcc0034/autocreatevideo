import assert from 'node:assert/strict'
import { unusedLanguageForm } from './fixtures/executionLanguage.ts'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp, createRenderer, h, nextTick, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createRouter, createMemoryHistory } from 'vue-router'

const root = fileURLToPath(new URL('..', import.meta.url))
const config = { deployment_id: 'deployment', operation: 'video.reference_to_video', mode: 'Ref2VA', prompt_mode: 'profile', prompt_profile_version_id: 'profile-21', workflow_version_id: 'workflow-1',
  inputs: [{ key: 'references', position: 0, kind: 'image', required: true, min_items: 1, max_items: 3,
    semantic_contract: { schema_version: 1, allowed_purposes: [], allowed_subject_kinds: ['character'], allowed_scope_kinds: ['request'], paired_with_input_key: null, allow_repeated_asset: false },
    targets: [{ target_kind: 'workflow_slot', target_key: 'images', is_primary: true }] }],
  parameters: [{ key: 'sample.scale', value_type: 'number', minimum: 0, maximum: 1, enum_values: [], step: null, remainder: null, policy: 'overridable', default_value: 0, locked_value: null, seed_policy: null }], auxiliaries: [] }
const version = { id: 'version', instance_id: 'instance', instance_name: 'test', version: 2, revision: 3, status: 'draft', ...config,
  prompt_operation: 'video.generate', prompt_profile_mode: 'Ref2VA', published_at: null, published_by_id: null, created_at: 'created', updated_at: 'updated', configuration: config }
const choices = { prompt_modes: [{ value: 'profile', label: '策略组装' }, { value: 'manual', label: '人工完整提示词' }], version_statuses: [{ value: 'draft', label: '草稿' }],
  input_kinds: [{ value: 'image', label: '图片' }], target_kinds: [{ value: 'workflow_slot', label: '工作流槽位' }], parameter_types: [{ value: 'number', label: '数值' }], parameter_policies: [{ value: 'overridable', label: '允许运行时配置' }], seed_policies: [],
  identity_options: { subject_kinds: [{ value: 'character', label: '人物' }], scope_kinds: [{ value: 'request', label: '本次请求' }] }, target: null }

async function moduleTest(run: (m: Record<string, any>, vite: any) => Promise<void> | void) {
  const vite = await createServer({ root, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false } })
  try { await run(await vite.ssrLoadModule('/src/services/executionRegistry.ts'), vite) } finally { await vite.close() }
}
test('execution registry preserves exact operation versus prompt purpose, nulls and zero', () => moduleTest(m => {
  const detail = m.adaptExecutionVersionDetail(version)
  assert.equal(detail.operation, 'video.reference_to_video')
  assert.equal(detail.prompt_operation, 'video.generate')
  assert.deepEqual(detail.configuration, config)
  assert.equal(detail.configuration.parameters[0].default_value, 0)
  assert.equal(detail.configuration.parameters[0].locked_value, null)
}))

test('context directory preserves declared related reviews and rejects incomplete metadata', () => moduleTest(m => {
  const field = { selector_key: 'shot.dialogue_with_speakers', label: '对白与说话人', kind: 'json', scope_kind: 'shot',
    revision_kind: 'storyboard_approval', description: '同时核对分镜与 Bible', value_schema: { type: 'array' },
    revision_dependencies: [{ key: 'bible', revision_kind: 'bible_approval', stage_key: 'bible_review' }] }
  const contract = { version_id: 'version', revision: 1, frozen: true, schema_version: 1, size_bytes: 100,
    appearance_inputs: { primary_inputs: [], difference_sources: [] },
    sha256: 'definition', target_fingerprint: 'target', definition: {}, context_fields: [field], input_forms: {}, subject_categories: [{value:'character',label:'人物'},{value:'scene',label:'场景'},{value:'prop',label:'道具'}], language_form: unusedLanguageForm() }
  assert.deepEqual(m.adaptExecutionContract(contract).context_fields, [field])
  assert.throws(() => m.adaptExecutionContract({ ...contract, context_fields: [{ ...field, revision_dependencies: null }] }), /响应不完整/)
  assert.throws(() => m.adaptExecutionContract({ ...contract, context_fields: [{ ...field,
    revision_dependencies: [{ key: 'bible', revision_kind: 'bible_approval' }] }] }), /stage_key/)
}))
test('registry does not invent missing metadata, page counts or resource identities', () => moduleTest(m => {
  for (const field of ['revision', 'configuration', 'prompt_operation', 'workflow_version_id']) {
    const broken = { ...version }; delete broken[field]
    assert.throws(() => m.adaptExecutionVersionDetail(broken), /响应不完整/)
  }
  assert.throws(() => m.adaptExecutionVersionDetail({ ...version, configuration: { ...config, operation: 'video.generate' } }), /身份不一致/)
  assert.throws(() => m.adaptExecutionVersion({ ...version, prompt_mode: 'manual' }), /不能带策略引用/)
  assert.throws(() => m.adaptExecutionVersion({ ...version, prompt_operation: null }), /提示词用途/)
  for (const count of [null, '2', true, -1]) assert.throws(() => m.adaptExecutionPage({ count, next: null, previous: null, results: [] }, m.adaptExecutionVersion), /count/)
  assert.deepEqual(m.adaptExecutionPage({ count: 0, next: null, previous: null, results: [] }, m.adaptExecutionVersion), { count: 0, next: null, previous: null, results: [] })
}))
test('draft patch sends only changed configuration and original revision', () => moduleTest(m => {
  const original = m.adaptExecutionVersionDetail(version)
  assert.deepEqual(m.executionDraftPatch(original, structuredClone(config)), { expected_revision: 3 })
  const changed = structuredClone(config); changed.parameters[0].default_value = 0.5
  assert.deepEqual(m.executionDraftPatch(original, changed), { expected_revision: 3, parameters: changed.parameters })
  assert.equal(original.configuration.parameters[0].default_value, 0)
  assert.throws(() => m.executionDraftPatch({ ...original, status: 'published' }, changed), /只能查看/)
  assert.throws(() => m.adaptExecutionConfiguration({ ...config, parameters: [{ ...config.parameters[0], minimum: NaN }] }), /minimum/)
  assert.throws(() => m.adaptExecutionConfiguration({ ...config, inputs: [{ ...config.inputs[0], min_items: true }] }), /min_items/)
}))

test('logical input structure survives read, true-difference patch, and explicit removal', () => moduleTest(m => {
  const configured = structuredClone(config) as any
  configured.inputs[0] = { ...configured.inputs[0], kind: 'json', data_structure_version_id: 'structure-v3' }
  const detail = m.adaptExecutionVersionDetail({ ...version, configuration: configured })
  assert.equal(detail.configuration.inputs[0].data_structure_version_id, 'structure-v3')
  assert.deepEqual(m.executionDraftPatch(detail, structuredClone(configured)), { expected_revision: 3 })
  const changed = structuredClone(configured)
  changed.inputs[0].data_structure_version_id = 'structure-v4'
  assert.deepEqual(m.executionDraftPatch(detail, changed), { expected_revision: 3, inputs: changed.inputs })
  changed.inputs[0].data_structure_version_id = null
  assert.equal(m.adaptExecutionConfiguration(changed).inputs[0].data_structure_version_id, null)
  assert.deepEqual(m.executionDraftPatch(detail, changed), { expected_revision: 3, inputs: changed.inputs })
  assert.equal(Object.hasOwn(m.adaptExecutionConfiguration(config).inputs[0], 'data_structure_version_id'), false)
  changed.inputs[0].data_structure_version_id = 3
  assert.throws(() => m.adaptExecutionConfiguration(changed), /data_structure_version_id/)
}))

test('logical structure selection is in the original editor, not on Profile derived ports', () => moduleTest(async (_m, vite) => {
  const { default: Component } = await vite.ssrLoadModule('/src/components/ExecutionConfigurationEditor.vue')
  const input = { ...config.inputs[0], key: 'custom', kind: 'json',
    targets: [{ target_kind: 'operation_input', target_key: 'payload', is_primary: true }] }
  const props = { modelValue: { ...config, inputs: [input] }, choices, auxiliaryVersions: [], options: null }
  const html = await renderToString(createSSRApp(Component, props))
  assert.match(html, /通用输入结构（可选）/)
  assert.match(html, /未选择时不增加业务结构约束/)
  const derived = { ...input, key: 'request.theme', kind: 'text',
    targets: [{ target_kind: 'profile_input', target_key: 'request.theme', is_primary: false }] }
  const derivedHtml = await renderToString(createSSRApp(Component, { ...props,
    modelValue: { ...config, inputs: [derived] }, options: { derived_input_keys: ['request.theme'], auxiliary_purposes: [], input_purposes: {} } }))
  assert.doesNotMatch(derivedHtml, /通用输入结构（可选）/)
}))

test('editor reports pending and completed saves before replacement can strand the dialog lock', () => moduleTest(async (_m, vite) => {
  const { executionRegistryApi: client } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { default: Component } = await vite.ssrLoadModule('/src/components/ExecutionVersionEditor.vue')
  client.choices = async () => choices
  client.authoring = async () => ({ auxiliary_purposes: [] })
  platformApi.listPromptProfileVersions = async () => []
  platformApi.listWorkflowVersions = async () => []
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  let state: any, finish: (result: unknown) => void, reject: (error: Error) => void
  let parentBusy = true, saves = 0, uncertain = true
  const events: boolean[] = []
  const app = renderer.createApp({ setup() {
    state = Component.setup({ instance: { id: 'instance', revision: 1 }, version, deployments: [] }, { expose() {}, emit(event: string, value: any) {
      if (event === 'processing') { parentBusy = value; events.push(value) }
      if (event === 'uncertain') uncertain = value
      if (event === 'saved') saves++
    } })
    return () => null
  } })
  app.provide(ssrContextKey, {})
  app.mount({})
  try {
    assert.equal(parentBusy, false, 'new editor must reset a stale parent lock')
    assert.equal(uncertain, false, 'replacement resets the previous unknown result')
    for (let i = 0; i < 10; i++) { await Promise.resolve(); await nextTick() }
    state.draft.value.parameters[0].default_value = 0.5
    client.updateVersion = () => new Promise((resolve, fail) => { finish = resolve; reject = fail })
    const saved = state.save()
    assert.equal(parentBusy, true, 'closing is blocked as soon as a request starts')
    finish!({ ...version, revision: 4 })
    await saved
    assert.equal(parentBusy, false, 'completion must not wait for a watcher that a remount may dispose')
    assert.equal(saves, 1)
    const failed = state.save()
    assert.equal(parentBusy, true)
    reject!(new Error('save failed'))
    await failed
    assert.equal(parentBusy, false)
    assert.equal(saves, 1, 'failed save cannot emit a saved result')
    assert.equal(uncertain, true, 'transport errors keep the close warning truthful')
    assert.equal(state.draft.value.parameters[0].default_value, 0.5, 'failed save preserves edits')
    assert.deepEqual(events, [false, true, false, true, false])
  } finally { app.unmount() }
}))

test('changing execution combinations uses explicit non-blocking confirmation and preserves cancelled edits', () => moduleTest(async (_m, vite) => {
  const { executionRegistryApi: client } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const { default: Component } = await vite.ssrLoadModule('/src/components/ExecutionVersionEditor.vue')
  client.choices = async () => choices
  client.authoring = async () => ({ auxiliary_purposes: [] })
  platformApi.listPromptProfileVersions = async () => []
  platformApi.listWorkflowVersions = async () => []
  platformApi.getPromptProfileOptions = async () => ({ sourceSelectors: [] })
  let writes = 0
  client.updateVersion = async () => { writes++; throw new Error('must not save implicitly') }
  const renderer = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  let state: any
  const app = renderer.createApp({ setup() {
    state = Component.setup({ instance: { id: 'instance', revision: 1 }, version, deployments: [] }, { expose() {}, emit() {} })
    return () => null
  } })
  app.provide(ssrContextKey, {}); app.mount({})
  try {
    for (let i = 0; i < 10; i++) { await Promise.resolve(); await nextTick() }
    const before = JSON.stringify(state.draft.value)
    await state.changedSelection('prompt_profile_version_id', 'new-profile')
    assert.equal(state.pendingSelection.value.value, 'new-profile')
    assert.equal(state.canSave.value, false)
    assert.equal(JSON.stringify(state.draft.value), before)
    state.cancelSelection()
    assert.equal(state.pendingSelection.value, null)
    assert.equal(JSON.stringify(state.draft.value), before)
    await state.changedSelection('prompt_profile_version_id', 'new-profile')
    await state.confirmSelection()
    assert.equal(state.draft.value.prompt_profile_version_id, 'new-profile')
    assert.deepEqual(state.draft.value.inputs, [])
    assert.deepEqual(state.draft.value.parameters, [])
    assert.equal(state.options.value, null, 'old catalog must not describe the new selection')
    assert.equal(state.pendingSelection.value, null)
    assert.equal(writes, 0, 'neither confirmation nor cancellation persists or invokes a model')
    assert.deepEqual(version.configuration, config)
  } finally { app.unmount() }
}))

test('frozen contract review is lazy read-only data rather than recursive disabled editors', () => moduleTest(async (_m, vite) => {
  const source = await readFile(new URL('../src/components/ExecutionInstanceRegistry.vue', import.meta.url), 'utf8')
  assert.match(source, /<ReadonlyDataValue[^>]+:key="contract.sha256"[^>]+:value="contract.definition"/)
  assert.doesNotMatch(source, /StructuredJsonEditor/)
  const component = (await vite.ssrLoadModule('/src/components/ReadonlyDataValue.vue')).default
  const contract = { interface: { profile_inputs: { 'project.logline': { source_reader: { protocol: 'project_snapshot/v1' } } } }, configuration: config }
  const html = await renderToString(createSSRApp({ render: () => h(component, { value: contract, label: '完整合同（只读）' }) }))
  assert.match(html, /完整合同（只读）/)
  assert.doesNotMatch(html, /<input|<textarea|<select|<pre/, 'closed review must not mount recursive form controls or serialize all data')
}))

test('registered text capabilities have explicit names in deployment evidence', () => moduleTest(async (_m, vite) => {
  const { operationLabel } = await vite.ssrLoadModule('/src/services/presentationLabels.ts')
  assert.equal(operationLabel('prompt.fields.generate'), '提示词字段生成（prompt.fields.generate）')
  assert.equal(operationLabel('prompt.fields.translate'), '提示词字段翻译（prompt.fields.translate）')
  assert.equal(operationLabel('prompt.draft'), '提示词起草（prompt.draft）')
  assert.equal(operationLabel('text.structured'), '通用结构化文本（text.structured）')
}))
test('publishing or retiring requires the exact reviewed revision and frozen state', () => moduleTest(m => {
  const contract = { version_id: version.id, revision: version.revision, frozen: false, sha256: 'exact-sha' }
  assert.deepEqual(m.executionLifecyclePayload(version, contract), { expected_revision: 3, expected_definition_sha256: 'exact-sha' })
  for (const bad of [null, { ...contract, version_id: 'other' }, { ...contract, revision: 4 }, { ...contract, frozen: true }]) assert.throws(() => m.executionLifecyclePayload(version, bad), /精确版本合同/)
  assert.throws(() => m.executionLifecyclePayload({ ...version, status: 'published' }, contract), /精确版本合同/)
  assert.equal(m.executionLifecyclePayload({ ...version, status: 'retired' }, { ...contract, frozen: true }).expected_definition_sha256, 'exact-sha')
}))
test('selection catalog labels come from server and changed selection invalidates authoring context', () => moduleTest(m => {
  assert.deepEqual(m.adaptExecutionSelectionOptions(choices), choices)
  assert.throws(() => m.adaptExecutionSelectionOptions({ ...choices, target: {} }), /响应不完整/)
  assert.equal(m.executionSelectionMatches(config, structuredClone(config)), true)
  for (const field of ['deployment_id', 'workflow_version_id', 'operation', 'mode', 'prompt_mode', 'prompt_profile_version_id']) assert.equal(m.executionSelectionMatches(config, { ...config, [field]: 'different' }), false)
  assert.equal(m.executionSelectionMatches(config, { ...config, inputs: [] }), true)
}))

test('operation output protocols are server-owned, required metadata, not visual-only inference', () => moduleTest(m => {
  const target = { deployment_id: 'd', model_profile_version_id: 'm', model_family: 'text', driver: 'openai_compatible', workflow_required: false, workflow: null,
    operations: [{ value: 'text.structured', label: '通用结构化文本', output_parser_keys: ['structured_json.v1'] }, { value: 'prompt.draft', label: '起草', output_parser_keys: [] }], modes: [] }
  assert.deepEqual(m.adaptExecutionSelectionOptions({ ...choices, target }).target, target)
  const broken = structuredClone(target); delete broken.operations[0].output_parser_keys
  assert.throws(() => m.adaptExecutionSelectionOptions({ ...choices, target: broken }), /output_parser_keys/)
}))

test('execution editor selects exact generic output, preserves same-operation edits and readonly references', () => moduleTest(async (_m, vite) => {
  const { default: Editor } = await vite.ssrLoadModule('/src/components/ExecutionVersionEditor.vue')
  const { businessOutputs } = await vite.ssrLoadModule('/src/services/businessOutputs.ts')
  const selected = { id: 'output-a', status: 'published', name: 'Layout A', version: 2, parser_key: 'structured_json.v1' }
  businessOutputs.detail = async id => ({ ...selected, id })
  let state
  const draft = { ...version, operation: 'text.structured', prompt_mode: 'manual', configuration: { ...config, operation: 'text.structured', prompt_mode: 'manual', inputs: [], parameters: [], auxiliaries: [] } }
  await renderToString(createSSRApp({ setup() { state = Editor.setup({ instance: { id: 'instance' }, version: draft, deployments: [] }, { expose() {}, emit() {} }); return () => h('div') } }))
  state.choices.value = { ...choices, target: { operations: [{ value: 'text.structured', output_parser_keys: ['structured_json.v1'] }], modes: [] } }
  state.chooseOutput({ ...selected, parser_key: 'visual_rules.v2' }); assert.equal(state.draft.value.output_version_id, undefined)
  state.chooseOutput({ ...selected, status: 'draft' }); assert.equal(state.draft.value.output_version_id, undefined)
  state.chooseOutput(selected); assert.equal(state.draft.value.output_version_id, selected.id)
  await state.changedSelection('prompt_mode', 'profile'); assert.equal(state.draft.value.output_version_id, selected.id)
  await state.changedSelection('operation', 'prompt.draft'); assert.equal(state.draft.value.output_version_id, undefined)
  const readonly = { ...draft, status: 'published', configuration: { ...draft.configuration, output_version_id: selected.id } }
  const html = await renderToString(createSSRApp(Editor, { instance: { id: 'instance' }, version: readonly, deployments: [] }))
  assert.match(html, /确切业务输出版本/); assert.match(html, /output-a/); assert.match(html, /查看输出版本/)
}))
test('API keeps server pagination and sends reviewed lifecycle identity without execution', () => moduleTest(async (m, vite) => {
  const { api } = await vite.ssrLoadModule('/src/services/api.ts')
  const calls: { method: string; args: any[] }[] = []
  api.get = async (...args) => { calls.push({ method: 'get', args }); return { data: { count: 101, next: '?page=2', previous: null, results: [version] } } }
  api.post = async (...args) => { calls.push({ method: 'post', args }); return { data: { ...version, status: 'published' } } }
  const { executionRegistryApi: client } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const page = await client.versions(1, { instance: 'instance', status: 'draft' })
  assert.equal(page.count, 101); assert.equal(page.next, '?page=2'); assert.equal(page.results.length, 1)
  assert.deepEqual(calls[0], { method: 'get', args: ['/model-execution-instance-versions/', { params: { page: 1, instance: 'instance', status: 'draft' } }] })
  await client.publish('version', { expected_revision: 3, expected_definition_sha256: 'exact-sha' })
  assert.deepEqual(calls[1], { method: 'post', args: ['/model-execution-instance-versions/version/publish/', { expected_revision: 3, expected_definition_sha256: 'exact-sha' }] })
  assert.equal(calls.length, 2)
}))
test('read-only form renders stored wiring and limits without current-resource replacement', () => moduleTest(async (_m, vite) => {
  const component = (await vite.ssrLoadModule('/src/components/ExecutionConfigurationEditor.vue')).default
  const before = JSON.stringify(config)
  const html = await renderToString(createSSRApp({ render: () => h(component, { modelValue: config, options: null, choices, auxiliaryVersions: [], readonly: true }) }))
  assert.match(html, /references/); assert.match(html, /主执行槽位/); assert.match(html, /sample.scale/)
  assert.match(html, /disabled/); assert.doesNotMatch(html, /<button\b[^>]*>[\s\S]*?(添加输入|添加覆盖)[\s\S]*?<\/button>/)
  assert.equal(JSON.stringify(config), before)
}))

test('visual rules explain profile-only inputs and frozen project copies', () => moduleTest(async (_m, vite) => {
  const component = (await vite.ssrLoadModule('/src/components/ExecutionConfigurationEditor.vue')).default
  const value = { ...structuredClone(config), operation: 'visual_rules.generate', inputs: [] }
  const html = await renderToString(createSSRApp({ render: () => h(component, { modelValue: value, options: null, choices, auxiliaryVersions: [] }) }))
  assert.match(html, /request.theme/); assert.match(html, /request.style/)
  assert.match(html, /不附加整份剧本或人物库/)
  assert.match(html, /确认后冻结本次内容/)
  assert.deepEqual(value.inputs, [])
}))

test('source wiring shows exact input and frozen field usages without matching names', () => moduleTest(async (_m, vite) => {
  const component = (await vite.ssrLoadModule('/src/components/ExecutionConfigurationEditor.vue')).default
  const value = structuredClone(config)
  value.inputs[0] = { ...value.inputs[0], key: 'story_brief', kind: 'text', targets: [{ target_kind: 'profile_input', target_key: 'request.theme', is_primary: false }] }
  const options = { derived_input_keys: [], profile_inputs: { 'request.theme': { target_kind:'profile_input', target_key:'request.theme', fields:[
    {field_key:'different_theme_key', usage:'assembly_source'}, {field_key:'story_direction', usage:'generation_source'},
  ] } } }
  const before = JSON.stringify(value)
  const html = await renderToString(createSSRApp({render:()=>h(component,{modelValue:value,options,choices,auxiliaryVersions:[],readonly:true})}))
  for (const text of ['输入「story_brief」','策略来源「request.theme」','different_theme_key、story_direction','直接装配','辅助生成／翻译的主要输入','不是直接作为该字段的最终正文']) assert.ok(html.includes(text),text)
  assert.equal(JSON.stringify(value),before)
}))

test('derived request inputs are read-only and unavailable in the repeated wiring menu', () => moduleTest(async (_m, vite) => {
  const component = (await vite.ssrLoadModule('/src/components/ExecutionConfigurationEditor.vue')).default
  const value = structuredClone(config)
  value.inputs = [{...value.inputs[0], key:'request.theme', kind:'text', min_items:1, max_items:1,
    targets:[{target_kind:'profile_input', target_key:'request.theme', is_primary:false}]}]
  const options = {derived_input_keys:['request.theme'], auxiliary_purposes:[], input_purposes:{}, profile_inputs:{'request.theme':{
    target_kind:'profile_input', target_key:'request.theme', kind:'text', min_items:1, max_items:1,
    fields:[{field_key:'theme',usage:'assembly_source'}],
  }}}
  const before = JSON.stringify(value)
  const html = await renderToString(createSSRApp({render:()=>h(component,{modelValue:value,options,choices,auxiliaryVersions:[]})}))
  assert.match(html, /<fieldset\b[^>]*\bdisabled\b/)
  assert.match(html, /自动派生，只读/)
  assert.match(html, /对应关系来自精确策略/)
  assert.doesNotMatch(html, /移除输入|同时用于策略|<option[^>]*>[^<]*request.theme/)
  assert.equal(JSON.stringify(value), before)
}))

test('authoring contract requires server-owned derivation metadata', () => moduleTest(m => {
  const row = {version_id:'version',revision:1,resources:{},input_targets:{},profile_inputs:{},derived_input_keys:['request.theme'],
    input_purposes:{},static_semantics:{},parameters:{},prompt_channels:{},prompt_not_required:false,
    identity_options:choices.identity_options,auxiliary_purposes:[]}
  assert.deepEqual(m.adaptExecutionAuthoringOptions(row).derived_input_keys, ['request.theme'])
  for(const invalid of [undefined, null, {}, [12]]) {
    assert.throws(()=>m.adaptExecutionAuthoringOptions({...row,derived_input_keys:invalid}), /响应不完整/)
  }
}))

test('text and structured inputs expose server-owned context scopes without granting them implicitly', () => moduleTest(async (_m, vite) => {
  const component = (await vite.ssrLoadModule('/src/components/ExecutionConfigurationEditor.vue')).default
  const contextChoices = { ...choices, identity_options: { ...choices.identity_options,
    scope_kinds: [{ value: 'project', label: '真实项目' }, { value: 'shot', label: '真实镜头' }] } }
  for (const kind of ['text', 'json']) {
    const value = structuredClone(config)
    value.inputs[0] = { ...value.inputs[0], key: 'source', kind,
      semantic_contract: { ...value.inputs[0].semantic_contract, allowed_scope_kinds: [] },
      targets: [{ target_kind: 'operation_input', target_key: 'source', is_primary: true }] }
    const props = { modelValue: value, options: null, choices: contextChoices, auxiliaryVersions: [] }
    const html = await renderToString(createSSRApp({ render: () => h(component, props) }))
    assert.match(html, /上下文来源与范围/); assert.match(html, /真实项目/); assert.match(html, /真实镜头/)
    assert.doesNotMatch(html, /素材用途、主体、范围与配对/)
    assert.deepEqual(value.inputs[0].semantic_contract.allowed_scope_kinds, [])
    value.inputs[0].semantic_contract.allowed_scope_kinds = ['project']
    const saved = await renderToString(createSSRApp({ render: () => h(component, { ...props, readonly: true }) }))
    assert.match(saved, /type="checkbox" checked[^>]*>真实项目/)
    assert.match(saved, /<fieldset\b[^>]*\bdisabled\b/)
    assert.deepEqual(value.inputs[0].semantic_contract.allowed_scope_kinds, ['project'])
  }
}))

test('published editor reads frozen relationships, never current resource or draft catalogs', () => moduleTest(async (_m, vite) => {
  const { executionRegistryApi: client, loadExecutionEditorCatalogs } = await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const { platformApi } = await vite.ssrLoadModule('/src/services/platformApi.ts')
  const calls: unknown[][] = []
  client.choices = async (...args) => { calls.push(args); return choices }
  client.authoring = async () => assert.fail('Published configuration cannot load current authoring targets')
  const frozenInputs = { 'request.theme': { fields:[{field_key:'exact_frozen_field',usage:'assembly_source'}] } }
  client.contract = async id => { assert.equal(id,version.id); return {version_id:id,revision:version.revision,frozen:true,definition:{interface:{profile_inputs:frozenInputs}},input_forms:{}} }
  platformApi.listPromptProfileVersions = async () => assert.fail('Historical strategy does not need a current strategy catalog')
  platformApi.listWorkflowVersions = async () => assert.fail('Historical workflow does not need a current workflow catalog')
  const result = await loadExecutionEditorCatalogs({ ...version, status: 'published' }, config)
  assert.deepEqual(calls, [[]])
  assert.equal(result.options, null)
  assert.deepEqual(result.profiles, [])
  assert.deepEqual(result.workflows, [])
  assert.deepEqual(result.profileInputs, frozenInputs)
  for (const changed of [{version_id:'different'},{revision:version.revision+1},{frozen:false}]) {
    client.contract = async () => ({version_id:version.id,revision:version.revision,frozen:true,...changed,definition:{interface:{profile_inputs:frozenInputs}},input_forms:{}})
    await assert.rejects(loadExecutionEditorCatalogs({ ...version, status: 'published' }, config), /来源关系合同与当前执行版本不一致/)
  }
  client.contract = async () => ({version_id:version.id,revision:version.revision,frozen:true,definition:{interface:{}},input_forms:{}})
  await assert.rejects(loadExecutionEditorCatalogs({ ...version, status: 'published' }, config), /冻结 profile_inputs/)
  for (const prompt_mode of ['manual', 'none']) {
    const manual = { ...config, prompt_mode, prompt_profile_version_id: null }
    const read = await loadExecutionEditorCatalogs({ ...version, ...manual, status: 'published', configuration: manual }, manual)
    assert.deepEqual(read.profileInputs, {})
    assert.equal(read.options, null)
    client.contract = async () => ({version_id:version.id,revision:version.revision,frozen:true,definition:{interface:{profile_inputs:frozenInputs}},input_forms:{}})
    await assert.rejects(loadExecutionEditorCatalogs({ ...version, ...manual, status: 'published', configuration: manual }, manual), /非策略方式不应声明/)
    client.contract = async () => ({version_id:version.id,revision:version.revision,frozen:true,definition:{interface:{}},input_forms:{}})
  }
}))

test('relation navigation reads the precise off-page version and never opens an unrelated result', () => moduleTest(async (_m, vite) => {
  const {executionRegistryApi: client}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const {default: Component}=await vite.ssrLoadModule('/src/components/ExecutionInstanceRegistry.vue')
  const router=createRouter({history:createMemoryHistory(), routes:[{path:'/settings', component:{render:()=>null}}]})
  await router.push('/settings'); await router.isReady()
  let state:any
  const app=createSSRApp({setup(){state=Component.setup({deployments:[]},{expose(){}});return()=>h('div')}})
  app.use(router); await renderToString(app)
  const id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', other='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
  const calls:string[]=[], instance={id:'instance', name:'Exact owner', key:'owner', revision:1, is_active:true, description:''}
  client.version=async(key:string)=>{calls.push(key);return {...version,id:key,status:'published'}}
  client.instance=async()=>instance
  client.versions=async()=>({count:0,next:null,previous:null,results:[]})
  await router.push('/settings?execution_version='+id)
  await state.followVersionContext()
  assert.equal(state.detail.value.id,id); assert.equal(state.editorOpen.value,true)
  assert.deepEqual(calls,[id]); assert.equal(state.versions.value.results.length,0)
  await router.push('/settings?execution_version='+id+'&execution_view=smoke')
  await state.followVersionContext()
  assert.equal(state.evidenceVersion.value.id,id); assert.equal(state.editorOpen.value,false)
  state.evidenceDirty.value=true
  await state.followVersionContext()
  assert.match(state.contextError.value,/完成或关闭/); assert.equal(calls.length,2)
  state.evidenceDirty.value=false
  client.version=async()=>({...version,id:other})
  await state.followVersionContext()
  assert.match(state.contextError.value,/不一致/); assert.equal(state.editorOpen.value,false); assert.equal(state.evidenceVersion.value,null)
  let finish:any
  client.version=async()=>new Promise(resolve=>{finish=resolve})
  const pending=state.followVersionContext()
  await router.push('/settings?execution_version=invalid')
  await state.followVersionContext()
  finish({...version,id}); await pending
  assert.match(state.contextError.value,/无效/); assert.equal(state.editorOpen.value,false)
}))

test('draft deletion reuses impact review and closes stale deep links only after confirmed completion', () => moduleTest(async (_m, vite) => {
  const {executionRegistryApi: client}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const {default: Component}=await vite.ssrLoadModule('/src/components/ExecutionInstanceRegistry.vue')
  const router=createRouter({history:createMemoryHistory(), routes:[{path:'/settings', component:{render:()=>null}}]})
  await router.push('/settings'); await router.isReady()
  let state:any
  const app=createSSRApp({setup(){state=Component.setup({deployments:[]},{expose(){}});return()=>h('div')}})
  app.use(router); await renderToString(app)
  const id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
  const instance={id:'instance', name:'Owner', key:'owner', revision:1, is_active:true, description:''}
  state.selected.value=instance; state.detail.value={...version,id,status:'published'}
  state.reviewDeletion('version'); assert.equal(state.deletionTarget.value,null)
  state.detail.value={...version,id}; state.editorDirty.value=true
  state.reviewDeletion('version'); assert.equal(state.deletionTarget.value,null)
  state.editorDirty.value=false; state.reviewDeletion('version')
  assert.deepEqual(state.deletionTarget.value,{type:'model_execution_instance_version',id,title:'删除草稿 · test · v2'})
  state.deletionTarget.value=null // closing review must not remove or reset the draft
  assert.equal(state.detail.value.id,id)
  await router.push('/settings?section=models&execution_version='+id+'&execution_view=config')
  state.reviewDeletion('version')
  await state.followVersionContext(); assert.match(state.contextError.value,/完成或关闭/)
  const reads:any[]=[]
  client.instances=async(page:number)=>{reads.push(['instances',page]);return {count:1,next:null,previous:null,results:[instance]}}
  client.instance=async()=>instance
  client.versions=async(page:number)=>{reads.push(['versions',page]);return {count:0,next:null,previous:null,results:[]}}
  await state.deletionCompleted()
  assert.equal(state.deletionTarget.value,null); assert.equal(state.detail.value,null)
  assert.equal(state.editorOpen.value,false); assert.equal(state.contextError.value,'')
  assert.deepEqual(router.currentRoute.value.query,{section:'models'})
  assert.deepEqual(reads,[['instances',1],['versions',1]])
  assert.match(state.notice.value,/永久删除.*共享资源未删除/)
  state.reviewDeletion('instance'); assert.equal(state.deletionTarget.value,null)
  state.selected.value={...instance,is_active:false}; state.reviewDeletion('instance')
  assert.equal(state.deletionTarget.value.type,'model_execution_instance')
  client.instances=async()=>({count:0,next:null,previous:null,results:[]})
  await state.deletionCompleted()
  assert.equal(state.selected.value,null); assert.equal(state.versions.value,null)
}))

test('smoke navigation confirms unsaved changes in-page and never abandons a busy request', () => moduleTest(async (_m, vite) => {
  const {executionRegistryApi: client}=await vite.ssrLoadModule('/src/services/executionRegistryApi.ts')
  const {default: Component}=await vite.ssrLoadModule('/src/components/ExecutionInstanceRegistry.vue')
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/settings',component:{render:()=>null}}]})
  await router.push('/settings'); await router.isReady()
  let state:any
  const app=createSSRApp({setup(){state=Component.setup({deployments:[]},{expose(){}});return()=>h('div')}})
  app.use(router); await renderToString(app)
  const published={...version,status:'published'}
  const reads:string[]=[]
  client.version=async(id:string)=>{reads.push(id);return published}
  state.evidenceVersion.value=published; state.evidenceDirty.value=true
  state.closeEvidence()
  assert.equal(state.evidenceLeave.value,'close'); assert.equal(state.evidenceVersion.value.id,version.id)
  state.evidenceLeave.value=null // Continue editing or Escape preserves the form.
  assert.equal(state.evidenceDirty.value,true); assert.deepEqual(reads,[])
  state.evidenceConfiguration()
  assert.equal(state.evidenceLeave.value,'configuration'); assert.deepEqual(reads,[])
  state.evidenceBusy.value=true; state.confirmEvidenceLeave()
  assert.equal(state.evidenceVersion.value.id,version.id); assert.equal(state.evidenceDirty.value,true)
  state.evidenceBusy.value=false; state.confirmEvidenceLeave(); await nextTick()
  assert.equal(state.evidenceVersion.value,null); assert.equal(state.evidenceDirty.value,false)
  assert.equal(state.evidenceLeave.value,null); assert.equal(state.editorOpen.value,true)
  assert.deepEqual(reads,[version.id])
  state.evidenceVersion.value=published; state.evidenceBusy.value=true; state.closeEvidence()
  assert.equal(state.evidenceLeave.value,null); assert.equal(state.evidenceVersion.value.id,version.id)
  state.evidenceBusy.value=false; state.closeEvidence()
  assert.equal(state.evidenceVersion.value,null); assert.equal(state.evidenceLeave.value,null)
}))
